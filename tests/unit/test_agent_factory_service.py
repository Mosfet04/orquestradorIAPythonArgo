"""Testes unitários para AgentFactoryService (agno v2.5)."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.rag_config import RagConfig, SearchStrategy
from src.domain.ports.model_factory_port import InvalidModelConfigError
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService
from tests.fakes import FakeModelFactory


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
    @patch("src.infrastructure.runtime.agno.agent_factory_service.Agent")
    @patch("src.infrastructure.runtime.agno.agent_factory_service.MongoAgentDb")
    async def test_create_agent_success(self, mock_db, mock_agent, service):
        mock_agent.return_value = MagicMock()
        config = _make_config()
        agent = await service.create_agent(config)
        assert agent is not None
        mock_agent.assert_called_once()

    async def test_create_agent_invalid_model_raises(self, service):
        """Config recusada pela fábrica sobe como ``InvalidModelConfigError`` (o use case isola)."""
        service._model_factory = FakeModelFactory(invalid_models={"llama3.2:latest"})
        with pytest.raises(InvalidModelConfigError, match="marcado como inválido"):
            await service.create_agent(_make_config())

    @patch("src.infrastructure.runtime.agno.agent_factory_service.Agent")
    @patch("src.infrastructure.runtime.agno.agent_factory_service.MongoAgentDb")
    async def test_modelo_recebe_a_model_config_inteira_fora_do_event_loop(self, mock_db, mock_agent, service):
        """F2-02: campos novos chegam à fábrica; a criação (segredo file:, DNS) roda em thread."""
        factory = FakeModelFactory()
        service._model_factory = factory
        config = _make_config(
            model_params={"temperature": 0.2}, base_url="https://gw.example.invalid/v1", api_key_ref="env:GW_API_KEY"
        )

        await service.create_agent(config)

        assert factory.configs == [config.model_config]
        assert factory.on_event_loop == [False]
        assert mock_agent.call_args.kwargs["model"] is factory.models[0]

    @patch("src.infrastructure.runtime.agno.agent_factory_service.Agent")
    @patch("src.infrastructure.runtime.agno.agent_factory_service.MongoAgentDb")
    async def test_create_agent_with_tools(self, mock_db, mock_agent, service, mock_tool_repository):
        mock_agent.return_value = MagicMock()
        mock_tool_repository.get_tools_by_ids.return_value = [MagicMock(), MagicMock()]
        service._tool_factory.create_tools_from_configs.return_value = [MagicMock()]
        config = _make_config(tools_ids=["t1", "t2"])
        agent = await service.create_agent(config)
        assert agent is not None
        mock_tool_repository.get_tools_by_ids.assert_awaited_once_with(["t1", "t2"])

    @patch("src.infrastructure.runtime.agno.agent_factory_service.Agent")
    @patch("src.infrastructure.runtime.agno.agent_factory_service.MongoAgentDb")
    @patch("src.infrastructure.runtime.agno.agent_factory_service.Knowledge")
    @patch("src.infrastructure.runtime.agno.agent_factory_service.MongoVectorDb")
    async def test_create_agent_with_rag(self, mock_vdb, mock_knowledge, mock_db, mock_agent, service):
        mock_agent.return_value = MagicMock()
        service._embedder_factory.create_embedder.return_value = MagicMock()
        rag = RagConfig(active=True, model="m", factory_ia_model="ollama")
        config = _make_config(rag_config=rag)
        agent = await service.create_agent(config)
        assert agent is not None

    @patch("src.infrastructure.runtime.agno.agent_factory_service.Agent")
    @patch("src.infrastructure.runtime.agno.agent_factory_service.MongoAgentDb")
    async def test_create_agent_enables_message_persistence(self, mock_db, mock_agent, service):
        mock_agent.return_value = MagicMock()
        config = _make_config()
        await service.create_agent(config)

        call_kwargs = mock_agent.call_args[1]
        assert call_kwargs["store_history_messages"] is True
        assert call_kwargs["store_tool_messages"] is True
        assert call_kwargs["store_events"] is True


# ── rodada 2 (R3): embedder recusado pela fábrica vai ao log com o motivo ──


@pytest.mark.parametrize(
    ("strategy", "message"),
    [(SearchStrategy.SEMANTIC, "Erro ao criar RAG"), (SearchStrategy.HIERARCHICAL, "Erro ao criar tool hierárquica")],
)
async def test_embedder_recusado_vai_ao_log_com_o_motivo(
    strategy: SearchStrategy, message: str, tmp_path, monkeypatch: pytest.MonkeyPatch
):
    """``InvalidModelConfigError`` é texto nosso (sem segredo): o operador precisa saber o porquê."""
    from src.application.services.document_indexing_service import DocumentIndexingService
    from src.application.services.knowledge_search_factory import KnowledgeSearchFactory
    from src.infrastructure.parsers.text_document_parser import TextDocumentParser
    from src.infrastructure.runtime.agno import agent_factory_service
    from tests.fakes import FakeEmbedderFactory, InMemoryDocumentTreeRepository, InMemoryToolRepository, RecordingLogger

    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "manual.md").write_text("# T\n\nIntro.\n\n## A\n\nTexto A.\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(agent_factory_service, "MongoAgentDb", lambda **_: None)
    logger = RecordingLogger()
    embedders = FakeEmbedderFactory(invalid_models={"emb-recusado"})
    tree = InMemoryDocumentTreeRepository()
    summary = AsyncMock()
    summary.generate_summary.return_value = "resumo"
    service = AgentFactoryService(
        db_url="mongodb://test:27017",
        logger=logger,
        model_factory=FakeModelFactory(),
        embedder_factory=embedders,
        tool_factory=AsyncMock(),
        tool_repository=InMemoryToolRepository(),
        indexing_service=DocumentIndexingService(
            parser=TextDocumentParser(), tree_repository=tree, summary_generator=summary,
            embedder_factory=embedders, logger=logger,
        ),
        search_factory=KnowledgeSearchFactory(tree_repository=tree, logger=logger),
    )
    rag = RagConfig(
        active=True, doc_name="manual.md", model="emb-recusado", factory_ia_model="ollama", search_strategy=strategy
    )

    agent = await service.create_agent(_make_config(rag_config=rag))

    assert agent.id == "test-agent"
    warnings = [(r.message, r.context) for r in logger.records if r.level == "warning"]
    assert (
        message,
        {
            "agent_id": "test-agent",
            "error_type": "InvalidModelConfigError",
            "reason": "modelo 'emb-recusado' marcado como inválido no fake",
        },
    ) in warnings
