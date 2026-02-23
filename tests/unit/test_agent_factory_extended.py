"""Testes estendidos para AgentFactoryService — caminhos de erro e edge cases."""

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


class TestBuildToolsError:
    """Testa _build_tools quando ocorre exceção."""

    @patch("src.application.services.agent_factory_service.Agent")
    @patch("src.application.services.agent_factory_service.MongoAgentDb")
    async def test_build_tools_exception_returns_empty(self, mock_db, mock_agent, service, mock_logger):
        """Se _build_tools falhar, deve retornar lista vazia e logar warning."""
        mock_agent.return_value = MagicMock()
        # Fazer o repositório lançar exceção
        service._tool_repository.get_tools_by_ids = AsyncMock(
            side_effect=RuntimeError("db error")
        )
        config = _make_config(tools_ids=["t1"])
        agent = await service.create_agent(config)
        assert agent is not None
        mock_logger.warning.assert_any_call("Erro ao criar tools", error="db error")


class TestBuildKnowledgeEdgeCases:
    """Testa _build_knowledge em caminhos de erro."""

    @patch("src.application.services.agent_factory_service.Agent")
    @patch("src.application.services.agent_factory_service.MongoAgentDb")
    async def test_rag_without_model_returns_none(self, mock_db, mock_agent, service, mock_logger):
        """RAG ativo sem factory_ia_model deve logar warning e ignorar."""
        mock_agent.return_value = MagicMock()
        rag = RagConfig(active=True, model="", factory_ia_model="")
        config = _make_config(rag_config=rag)
        agent = await service.create_agent(config)
        assert agent is not None
        mock_logger.warning.assert_any_call(
            "RAG ativo sem factory_ia_model ou model — ignorando"
        )

    @patch("src.application.services.agent_factory_service.Agent")
    @patch("src.application.services.agent_factory_service.MongoAgentDb")
    async def test_rag_without_factory_ia_model(self, mock_db, mock_agent, service, mock_logger):
        """RAG ativo com model mas sem factory_ia_model deve ignorar."""
        mock_agent.return_value = MagicMock()
        rag = RagConfig(active=True, model="embed-v1", factory_ia_model="")
        config = _make_config(rag_config=rag)
        agent = await service.create_agent(config)
        assert agent is not None


class TestLoadDocument:
    """Testa _load_document em caminhos de erro."""

    @patch("src.application.services.agent_factory_service.Agent")
    @patch("src.application.services.agent_factory_service.MongoAgentDb")
    @patch("src.application.services.agent_factory_service.Knowledge")
    @patch("src.application.services.agent_factory_service.MongoVectorDb")
    async def test_load_document_no_doc_name(
        self, mock_vdb, mock_knowledge_cls, mock_db, mock_agent, service, mock_logger
    ):
        """Sem doc_name, deve logar info e não inserir."""
        mock_agent.return_value = MagicMock()
        service._embedder_factory.create_model.return_value = MagicMock()
        knowledge_instance = MagicMock()
        mock_knowledge_cls.return_value = knowledge_instance

        rag = RagConfig(active=True, model="m", factory_ia_model="ollama", doc_name="")
        config = _make_config(rag_config=rag)
        agent = await service.create_agent(config)
        assert agent is not None
        mock_logger.info.assert_any_call("Nenhum documento especificado para RAG")

    @patch("src.application.services.agent_factory_service.Agent")
    @patch("src.application.services.agent_factory_service.MongoAgentDb")
    @patch("src.application.services.agent_factory_service.Knowledge")
    @patch("src.application.services.agent_factory_service.MongoVectorDb")
    async def test_load_document_file_not_found(
        self, mock_vdb, mock_knowledge_cls, mock_db, mock_agent, service, mock_logger
    ):
        """FileNotFoundError deve logar warning."""
        mock_agent.return_value = MagicMock()
        service._embedder_factory.create_model.return_value = MagicMock()
        knowledge_instance = MagicMock()
        knowledge_instance.insert.side_effect = FileNotFoundError("not found")
        mock_knowledge_cls.return_value = knowledge_instance

        rag = RagConfig(active=True, model="m", factory_ia_model="ollama", doc_name="test.pdf")
        config = _make_config(rag_config=rag)
        agent = await service.create_agent(config)
        assert agent is not None
        mock_logger.warning.assert_any_call(
            "Documento não encontrado", path="docs/test.pdf"
        )

    @patch("src.application.services.agent_factory_service.Agent")
    @patch("src.application.services.agent_factory_service.MongoAgentDb")
    @patch("src.application.services.agent_factory_service.Knowledge")
    @patch("src.application.services.agent_factory_service.MongoVectorDb")
    async def test_load_document_generic_exception(
        self, mock_vdb, mock_knowledge_cls, mock_db, mock_agent, service, mock_logger
    ):
        """Exceção genérica ao inserir documento deve logar error."""
        mock_agent.return_value = MagicMock()
        service._embedder_factory.create_model.return_value = MagicMock()
        knowledge_instance = MagicMock()
        knowledge_instance.insert.side_effect = RuntimeError("unknown error")
        mock_knowledge_cls.return_value = knowledge_instance

        rag = RagConfig(active=True, model="m", factory_ia_model="ollama", doc_name="test.pdf")
        config = _make_config(rag_config=rag)
        agent = await service.create_agent(config)
        assert agent is not None
        mock_logger.error.assert_any_call(
            "Erro ao carregar documento RAG",
            path="docs/test.pdf",
            error="unknown error",
        )
