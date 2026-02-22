"""Testes unitários para AgentFactoryService (agno v2.5)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.application.services.agent_factory_service import AgentFactoryService
from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.rag_config import RagConfig


def _make_config(**overrides) -> AgentConfig:
    defaults = dict(
        id="test-agent",
        nome="Agente Teste",
        factory_ia_model="ollama",
        model="llama3.2:latest",
        descricao="desc",
        prompt="prompt",
    )
    defaults.update(overrides)
    return AgentConfig(**defaults)


@pytest.fixture
def service(mock_logger, mock_tool_repository):
    model_factory = MagicMock()
    model_factory.validate_model_config.return_value = {"valid": True, "errors": []}
    model_factory.create_model.return_value = MagicMock()

    embedder_factory = MagicMock()
    tool_factory = AsyncMock()
    tool_factory.create_tools_from_configs = AsyncMock(return_value=[])

    return AgentFactoryService(
        db_url="mongodb://test:27017",
        db_name="test_db",
        logger=mock_logger,
        model_factory=model_factory,
        embedder_factory=embedder_factory,
        tool_factory=tool_factory,
        tool_repository=mock_tool_repository,
    )


class TestAgentFactoryService:
    @patch("src.application.services.agent_factory_service.Agent")
    @patch("src.application.services.agent_factory_service.MongoAgentDb")
    async def test_create_agent_success(self, mock_db, mock_agent, service):
        mock_agent.return_value = MagicMock()
        config = _make_config()
        agent = await service.create_agent(config)
        assert agent is not None
        mock_agent.assert_called_once()

    async def test_create_agent_invalid_model_raises(self, service):
        service._model_factory.validate_model_config.return_value = {
            "valid": False,
            "errors": ["Modelo inválido"],
        }
        config = _make_config(factory_ia_model="invalid")
        with pytest.raises(ValueError, match="Configuração de modelo inválida"):
            await service.create_agent(config)

    @patch("src.application.services.agent_factory_service.Agent")
    @patch("src.application.services.agent_factory_service.MongoAgentDb")
    async def test_create_agent_with_tools(self, mock_db, mock_agent, service, mock_tool_repository):
        mock_agent.return_value = MagicMock()
        mock_tool_repository.get_tools_by_ids.return_value = [MagicMock(), MagicMock()]
        service._tool_factory.create_tools_from_configs.return_value = [MagicMock()]
        config = _make_config(tools_ids=["t1", "t2"])
        agent = await service.create_agent(config)
        assert agent is not None
        mock_tool_repository.get_tools_by_ids.assert_awaited_once_with(["t1", "t2"])

    @patch("src.application.services.agent_factory_service.Agent")
    @patch("src.application.services.agent_factory_service.MongoAgentDb")
    @patch("src.application.services.agent_factory_service.Knowledge")
    @patch("src.application.services.agent_factory_service.MongoVectorDb")
    async def test_create_agent_with_rag(self, mock_vdb, mock_knowledge, mock_db, mock_agent, service):
        mock_agent.return_value = MagicMock()
        service._embedder_factory.create_model.return_value = MagicMock()
        rag = RagConfig(active=True, model="m", factory_ia_model="ollama")
        config = _make_config(rag_config=rag)
        agent = await service.create_agent(config)
        assert agent is not None
