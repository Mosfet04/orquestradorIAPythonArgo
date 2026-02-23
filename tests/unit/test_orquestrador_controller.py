"""Testes para OrquestradorController."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.presentation.controllers.orquestrador_controller import OrquestradorController


@pytest.fixture
def controller(mock_logger):
    agents_use_case = AsyncMock()
    agents_use_case.execute = AsyncMock(return_value=[MagicMock(), MagicMock()])
    teams_use_case = AsyncMock()
    teams_use_case.execute = AsyncMock(return_value=[])
    return OrquestradorController(
        get_active_agents_use_case=agents_use_case,
        get_active_teams_use_case=teams_use_case,
        logger=mock_logger,
    )


class TestOrquestradorController:
    async def test_get_agents_first_call(self, controller):
        agents = await controller.get_agents()
        assert len(agents) == 2

    async def test_get_agents_uses_cache(self, controller):
        await controller.get_agents()
        await controller.get_agents()
        # use_case.execute só deve ser chamado uma vez (cache)
        assert controller._agents_use_case.execute.await_count == 1

    async def test_refresh_clears_cache(self, controller):
        await controller.get_agents()
        await controller.refresh_agents()
        assert controller._agents_use_case.execute.await_count == 2

    async def test_warm_up_cache(self, controller):
        await controller.warm_up_cache()
        assert controller._cache is not None

    def test_cache_stats_empty(self, controller):
        stats = controller.get_cache_stats()
        assert stats["agents"]["status"] == "empty"
        assert stats["teams"]["status"] == "empty"

    async def test_cache_stats_active(self, controller):
        await controller.get_agents()
        stats = controller.get_cache_stats()
        assert stats["agents"]["status"] == "active"
        assert stats["agents"]["agent_count"] == 2

    async def test_get_agents_fallback_on_error(self, controller):
        # Primeiro carregamento com sucesso
        await controller.get_agents()
        # Segundo carregamento falha — deve usar cache expirado
        controller._agents_use_case.execute = AsyncMock(side_effect=RuntimeError("fail"))
        # Forçar expiração
        controller._cache = None
        with pytest.raises(RuntimeError):
            await controller.get_agents()
