"""Testes estendidos para OrquestradorController — teams cache, fallbacks."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.presentation.controllers.orquestrador_controller import (
    AgentCacheEntry,
    OrquestradorController,
    TeamCacheEntry,
)


# ── TeamCacheEntry ───────────────────────────────────────────────────


class TestTeamCacheEntry:
    def test_initial_state(self):
        teams = [MagicMock(), MagicMock()]
        entry = TeamCacheEntry(teams, ttl_minutes=5)
        assert entry.hit_count == 0
        assert entry.teams == teams
        assert not entry.is_expired()

    def test_is_expired(self):
        teams = [MagicMock()]
        entry = TeamCacheEntry(teams, ttl_minutes=0)
        # Forçar expiração
        entry.created_at = datetime.now(timezone.utc) - timedelta(minutes=1)
        assert entry.is_expired()

    def test_access_increments_hit(self):
        teams = [MagicMock()]
        entry = TeamCacheEntry(teams)
        result = entry.access()
        assert result == teams
        assert entry.hit_count == 1
        result2 = entry.access()
        assert entry.hit_count == 2

    def test_not_expired_within_ttl(self):
        entry = TeamCacheEntry([MagicMock()], ttl_minutes=10)
        assert not entry.is_expired()


# ── AgentCacheEntry (extra cobertura) ────────────────────────────────


class TestAgentCacheEntry:
    def test_access_updates_last_access(self):
        agents = [MagicMock()]
        entry = AgentCacheEntry(agents)
        before = entry.last_access
        entry.access()
        assert entry.last_access >= before
        assert entry.hit_count == 1


# ── OrquestradorController — teams ──────────────────────────────────


class TestOrquestradorControllerTeams:
    @pytest.fixture
    def controller(self, mock_logger):
        agents_uc = AsyncMock()
        agents_uc.execute = AsyncMock(return_value=[MagicMock()])
        teams_uc = AsyncMock()
        teams_uc.execute = AsyncMock(return_value=[MagicMock(), MagicMock()])
        return OrquestradorController(
            get_active_agents_use_case=agents_uc,
            get_active_teams_use_case=teams_uc,
            logger=mock_logger,
        )

    async def test_get_teams_first_call(self, controller):
        teams = await controller.get_teams()
        assert len(teams) == 2

    async def test_get_teams_uses_cache(self, controller):
        await controller.get_teams()
        teams = await controller.get_teams()
        # Segunda chamada deve usar cache
        assert len(teams) == 2
        assert controller._teams_use_case.execute.await_count == 1

    async def test_get_teams_cache_expired(self, controller):
        await controller.get_teams()
        # Forçar expiração
        controller._team_cache.created_at = datetime.now(timezone.utc) - timedelta(minutes=10)
        await controller.get_teams()
        assert controller._teams_use_case.execute.await_count == 2

    async def test_cache_stats_with_teams(self, controller):
        await controller.get_teams()
        stats = controller.get_cache_stats()
        assert stats["teams"]["status"] == "active"
        assert stats["teams"]["team_count"] == 2

    async def test_cache_stats_teams_empty(self, controller):
        stats = controller.get_cache_stats()
        assert stats["teams"]["status"] == "empty"


class TestLoadTeamsFallback:
    @pytest.fixture
    def controller(self, mock_logger):
        agents_uc = AsyncMock()
        agents_uc.execute = AsyncMock(return_value=[MagicMock()])
        teams_uc = AsyncMock()
        teams_uc.execute = AsyncMock(return_value=[MagicMock()])
        return OrquestradorController(
            get_active_agents_use_case=agents_uc,
            get_active_teams_use_case=teams_uc,
            logger=mock_logger,
        )

    async def test_load_teams_error_with_cache_fallback(self, controller, mock_logger):
        """Se _load_teams falha mas existe cache, deve retornar cache expirado."""
        # Primeiro carregamento OK
        await controller.get_teams()
        assert controller._team_cache is not None

        # Segundo falha
        controller._teams_use_case.execute = AsyncMock(side_effect=RuntimeError("fail"))
        # Forçar expiração do cache
        controller._team_cache.created_at = datetime.now(timezone.utc) - timedelta(minutes=10)

        teams = await controller.get_teams()
        # Deve retornar o cache expirado como fallback
        assert len(teams) == 1
        mock_logger.warning.assert_called()

    async def test_load_teams_error_no_cache_returns_empty(self, controller, mock_logger):
        """Se _load_teams falha sem cache, retorna lista vazia."""
        controller._teams_use_case.execute = AsyncMock(side_effect=RuntimeError("fail"))

        teams = await controller.get_teams()
        assert teams == []
        mock_logger.error.assert_called()


class TestLoadAgentsFallback:
    @pytest.fixture
    def controller(self, mock_logger):
        agents_uc = AsyncMock()
        agents_uc.execute = AsyncMock(return_value=[MagicMock(), MagicMock()])
        teams_uc = AsyncMock()
        teams_uc.execute = AsyncMock(return_value=[])
        return OrquestradorController(
            get_active_agents_use_case=agents_uc,
            get_active_teams_use_case=teams_uc,
            logger=mock_logger,
        )

    async def test_load_agents_error_with_cache_fallback(self, controller, mock_logger):
        """Se _load_agents falha com cache existente, retorna cache expirado."""
        await controller.get_agents()
        assert controller._cache is not None

        controller._agents_use_case.execute = AsyncMock(side_effect=RuntimeError("db down"))
        # Forçar expiração
        controller._cache.created_at = datetime.now(timezone.utc) - timedelta(minutes=10)

        agents = await controller.get_agents()
        assert len(agents) == 2
        mock_logger.warning.assert_any_call("Usando cache expirado como fallback")
