"""Testes para GetActiveAgentsUseCase."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase
from src.domain.entities.agent_config import AgentConfig


def _make_config(agent_id: str = "a1") -> AgentConfig:
    return AgentConfig(
        id=agent_id,
        nome="Agente",
        factory_ia_model="ollama",
        model="llama3.2:latest",
        descricao="desc",
        prompt="prompt",
    )


class TestGetActiveAgentsUseCase:
    async def test_execute_returns_agents(self, mock_agent_config_repository):
        configs = [_make_config("a1"), _make_config("a2")]
        mock_agent_config_repository.get_active_agents.return_value = configs

        mock_factory = AsyncMock()
        mock_agent = MagicMock()
        mock_factory.create_agent = AsyncMock(return_value=mock_agent)

        use_case = GetActiveAgentsUseCase(mock_factory, mock_agent_config_repository)
        agents = await use_case.execute()

        assert len(agents) == 2
        assert mock_factory.create_agent.await_count == 2

    async def test_execute_returns_empty_when_no_configs(self, mock_agent_config_repository):
        mock_agent_config_repository.get_active_agents.return_value = []
        mock_factory = AsyncMock()

        use_case = GetActiveAgentsUseCase(mock_factory, mock_agent_config_repository)
        agents = await use_case.execute()

        assert agents == []
        mock_factory.create_agent.assert_not_awaited()

    async def test_execute_skips_failed_agents(self, mock_agent_config_repository):
        configs = [_make_config("ok"), _make_config("fail")]
        mock_agent_config_repository.get_active_agents.return_value = configs

        mock_factory = AsyncMock()
        mock_agent = MagicMock()
        mock_factory.create_agent = AsyncMock(
            side_effect=[mock_agent, RuntimeError("boom")]
        )

        use_case = GetActiveAgentsUseCase(mock_factory, mock_agent_config_repository)
        agents = await use_case.execute()

        assert len(agents) == 1
