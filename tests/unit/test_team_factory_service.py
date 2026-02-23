"""Testes para TeamFactoryService."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from src.application.services.team_factory_service import TeamFactoryService
from src.domain.entities.team_config import TeamConfig


def _make_config(**overrides):
    base = {
        "id": "team-1",
        "nome": "Team Router",
        "factory_ia_model": "ollama",
        "model": "llama3.2:latest",
        "member_ids": ["agent-a", "agent-b"],
        "mode": "route",
    }
    base.update(overrides)
    return TeamConfig(**base)


def _make_agent(agent_id: str) -> MagicMock:
    agent = MagicMock()
    agent.id = agent_id
    return agent


@pytest.fixture
def mock_model_factory():
    factory = MagicMock()
    factory.create_model = MagicMock(return_value=MagicMock())
    return factory


@pytest.fixture
def service(mock_logger, mock_model_factory):
    return TeamFactoryService(
        db_url="mongodb://localhost:27017",
        db_name="testdb",
        logger=mock_logger,
        model_factory=mock_model_factory,
    )


class TestTeamFactoryServiceCreateTeam:
    @patch("src.application.services.team_factory_service.Team")
    @patch("src.application.services.team_factory_service.MongoAgentDb")
    def test_create_team_success(self, mock_db_cls, mock_team_cls, service):
        agents = [_make_agent("agent-a"), _make_agent("agent-b")]
        config = _make_config()

        mock_team_cls.return_value = MagicMock()
        result = service.create_team(config, agents)

        mock_team_cls.assert_called_once()
        assert result is mock_team_cls.return_value

    @patch("src.application.services.team_factory_service.Team")
    @patch("src.application.services.team_factory_service.MongoAgentDb")
    def test_create_team_with_coordinate_mode(self, mock_db_cls, mock_team_cls, service):
        agents = [_make_agent("agent-a"), _make_agent("agent-b")]
        config = _make_config(mode="coordinate")

        service.create_team(config, agents)

        call_kwargs = mock_team_cls.call_args[1]
        assert call_kwargs["respond_directly"] is False

    @patch("src.application.services.team_factory_service.Team")
    @patch("src.application.services.team_factory_service.MongoAgentDb")
    def test_create_team_with_route_mode_respond_directly(self, mock_db_cls, mock_team_cls, service):
        agents = [_make_agent("agent-a"), _make_agent("agent-b")]
        config = _make_config(mode="route")

        service.create_team(config, agents)

        call_kwargs = mock_team_cls.call_args[1]
        assert call_kwargs["respond_directly"] is True

    @patch("src.application.services.team_factory_service.Team")
    @patch("src.application.services.team_factory_service.MongoAgentDb")
    def test_create_team_passes_memory_settings(self, mock_db_cls, mock_team_cls, service):
        agents = [_make_agent("agent-a")]
        config = _make_config(
            member_ids=["agent-a"],
            user_memory_active=False,
            summary_active=True,
        )

        service.create_team(config, agents)

        call_kwargs = mock_team_cls.call_args[1]
        assert call_kwargs["enable_agentic_memory"] is False
        assert call_kwargs["enable_user_memories"] is False
        assert call_kwargs["enable_session_summaries"] is True

    @patch("src.application.services.team_factory_service.Team")
    @patch("src.application.services.team_factory_service.MongoAgentDb")
    def test_create_team_enables_message_persistence(self, mock_db_cls, mock_team_cls, service):
        agents = [_make_agent("agent-a"), _make_agent("agent-b")]
        config = _make_config()

        service.create_team(config, agents)

        call_kwargs = mock_team_cls.call_args[1]
        assert call_kwargs["store_history_messages"] is True
        assert call_kwargs["store_tool_messages"] is True
        assert call_kwargs["store_events"] is True
        assert call_kwargs["store_member_responses"] is True


class TestTeamFactoryServiceResolveMembers:
    def test_resolve_all_members(self, service):
        agents = [_make_agent("agent-a"), _make_agent("agent-b")]
        config = _make_config(member_ids=["agent-a", "agent-b"])

        result = service._resolve_members(config, agents)
        assert len(result) == 2

    def test_resolve_partial_members_logs_warning(self, service, mock_logger):
        agents = [_make_agent("agent-a")]
        config = _make_config(member_ids=["agent-a", "agent-missing"])

        result = service._resolve_members(config, agents)
        assert len(result) == 1
        mock_logger.warning.assert_called_once()

    def test_resolve_no_valid_members_raises(self, service):
        agents = [_make_agent("agent-x")]
        config = _make_config(member_ids=["agent-a", "agent-b"])

        with pytest.raises(ValueError, match="nenhum membro válido"):
            service._resolve_members(config, agents)
