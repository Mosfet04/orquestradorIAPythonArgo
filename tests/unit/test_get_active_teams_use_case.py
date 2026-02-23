"""Testes para GetActiveTeamsUseCase."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.application.use_cases.get_active_teams_use_case import GetActiveTeamsUseCase
from src.domain.entities.team_config import TeamConfig


def _make_team_config(team_id: str = "team-1") -> TeamConfig:
    return TeamConfig(
        id=team_id,
        nome=f"Team {team_id}",
        factory_ia_model="ollama",
        model="llama3.2:latest",
        member_ids=["agent-a"],
        mode="route",
    )


@pytest.fixture
def team_factory_service():
    service = MagicMock()
    service.create_team = MagicMock(return_value=MagicMock())
    return service


@pytest.fixture
def use_case(team_factory_service, mock_team_config_repository, mock_logger):
    return GetActiveTeamsUseCase(
        team_factory_service=team_factory_service,
        team_config_repository=mock_team_config_repository,
        logger=mock_logger,
    )


class TestGetActiveTeamsUseCase:
    async def test_execute_returns_teams(
        self, use_case, mock_team_config_repository, team_factory_service
    ):
        configs = [_make_team_config("t1"), _make_team_config("t2")]
        mock_team_config_repository.get_active_teams = AsyncMock(return_value=configs)
        agents = [MagicMock(), MagicMock()]

        result = await use_case.execute(agents)

        assert len(result) == 2
        assert team_factory_service.create_team.call_count == 2

    async def test_execute_empty_configs(
        self, use_case, mock_team_config_repository
    ):
        mock_team_config_repository.get_active_teams = AsyncMock(return_value=[])

        result = await use_case.execute([MagicMock()])

        assert result == []

    async def test_execute_skips_failed_team(
        self, use_case, mock_team_config_repository, team_factory_service, mock_logger
    ):
        configs = [_make_team_config("t1"), _make_team_config("t2")]
        mock_team_config_repository.get_active_teams = AsyncMock(return_value=configs)
        team_factory_service.create_team = MagicMock(
            side_effect=[ValueError("boom"), MagicMock()]
        )

        result = await use_case.execute([MagicMock()])

        assert len(result) == 1
        mock_logger.error.assert_called_once()

    async def test_execute_all_fail_returns_empty(
        self, use_case, mock_team_config_repository, team_factory_service
    ):
        configs = [_make_team_config("t1")]
        mock_team_config_repository.get_active_teams = AsyncMock(return_value=configs)
        team_factory_service.create_team = MagicMock(
            side_effect=RuntimeError("fail")
        )

        result = await use_case.execute([MagicMock()])

        assert result == []
