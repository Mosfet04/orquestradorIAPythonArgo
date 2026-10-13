"""Busca hierárquica: retrocompatibilidade da config e o que a estratégia muda no agente montado.

A parte da fábrica é comportamento sobre o ``Agent`` real do agno, montado pelo
``AgentFactoryService`` de produção com fakes nas portas (modelo, embedder, repositório de tools,
árvore de documentos em memória) e o serviço de indexação e a fábrica de busca reais; nada de método
privado da fábrica nem de ``MagicMock`` nas portas (resíduo (6) do F2-07, fechado no F2-08).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agno.tools import Toolkit

from src.application.services.document_indexing_service import DocumentIndexingService
from src.application.services.knowledge_search_factory import KnowledgeSearchFactory
from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.rag_config import RagConfig, SearchStrategy
from src.domain.ports.summary_generator_port import ISummaryGenerator
from src.infrastructure.parsers.text_document_parser import TextDocumentParser
from src.infrastructure.repositories.config_documents import map_agent_document
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from tests.fakes import (
    FakeEmbedderFactory,
    FakeModelFactory,
    InMemoryDocumentTreeRepository,
    InMemoryToolRepository,
    RecordingLogger,
)
from tests.fakes.session_db import fake_session_db


class TestAgentFactoryRetrocompat:
    """Garante que agentes existentes sem search_strategy continuam funcionando."""

    def _make_config(
        self, search_strategy: SearchStrategy = SearchStrategy.SEMANTIC
    ) -> AgentConfig:
        return AgentConfig(
            id="agent-1",
            nome="Test Agent",
            factory_ia_model="ollama",
            model="llama3.2:latest",
            descricao="Agent de teste",
            prompt="Você é um assistente.",
            rag_config=RagConfig(
                active=True,
                doc_name="basic-prog.txt",
                model="nomic-embed-text:latest",
                factory_ia_model="ollama",
                search_strategy=search_strategy,
            ),
        )

    def test_default_rag_config_uses_semantic(self):
        """RagConfig sem search_strategy explícito deve ser SEMANTIC."""
        config = RagConfig(active=True)
        assert config.search_strategy == SearchStrategy.SEMANTIC

    def test_agent_config_with_semantic_rag(self):
        config = self._make_config(SearchStrategy.SEMANTIC)
        assert config.rag_config.search_strategy == SearchStrategy.SEMANTIC

    def test_agent_config_with_hierarchical_rag(self):
        config = self._make_config(SearchStrategy.HIERARCHICAL)
        assert config.rag_config.search_strategy == SearchStrategy.HIERARCHICAL


class TestMongoAgentConfigRepositoryRetrocompat:
    """Testa deserialização com e sem search_strategy."""

    def test_map_without_search_strategy(self):
        """Documentos MongoDB antigos sem search_strategy devem funcionar."""

        data = {
            "id": "agent-1",
            "nome": "Agent",
            "model": "llama3.2:latest",
            "factory_ia_model": "ollama",
            "descricao": "Desc",
            "prompt": "Prompt",
            "active": True,
            "rag_config": {
                "active": True,
                "doc_name": "doc.txt",
                "model": "nomic-embed-text:latest",
                "factory_ia_model": "ollama",
                # sem search_strategy
            },
        }

        entity = map_agent_document(data, RecordingLogger())

        assert entity.rag_config is not None
        assert entity.rag_config.search_strategy == SearchStrategy.SEMANTIC
        assert entity.rag_config.active is True

    def test_map_with_search_strategy_hierarchical(self):

        data = {
            "id": "agent-2",
            "nome": "Agent 2",
            "model": "llama3.2:latest",
            "factory_ia_model": "ollama",
            "descricao": "Desc",
            "prompt": "Prompt",
            "active": True,
            "rag_config": {
                "active": True,
                "doc_name": "doc.txt",
                "model": "nomic-embed-text:latest",
                "factory_ia_model": "ollama",
                "search_strategy": "hierarchical",
            },
        }

        entity = map_agent_document(data, RecordingLogger())

        assert entity.rag_config.search_strategy == SearchStrategy.HIERARCHICAL

    def test_map_without_rag_config(self):

        data = {
            "id": "agent-3",
            "nome": "Agent 3",
            "model": "llama3.2:latest",
            "factory_ia_model": "ollama",
            "descricao": "Desc",
            "prompt": "Prompt",
            "active": True,
        }

        entity = map_agent_document(data, RecordingLogger())
        assert entity.rag_config is None


class _FixedSummary(ISummaryGenerator):
    async def generate_summary(self, content: str) -> str:
        return "resumo"


class _Assembly:
    """Fábrica de agentes de produção com fakes nas portas; a árvore de documentos fica à vista."""

    def __init__(self, *, hierarchical_services: bool = True) -> None:
        self.logger = RecordingLogger()
        self.tree = InMemoryDocumentTreeRepository()
        self.embedders = FakeEmbedderFactory(dimensions=8)
        self.service = AgentFactoryService(
            db_url="mongodb://mongo.test.invalid:27017",
            db_name="test_db",
            session_db=fake_session_db(),
            logger=self.logger,
            model_factory=FakeModelFactory(),
            embedder_factory=self.embedders,
            tool_factory=HttpToolFactory(logger=self.logger),
            tool_repository=InMemoryToolRepository(),
            indexing_service=DocumentIndexingService(
                parser=TextDocumentParser(),
                tree_repository=self.tree,
                summary_generator=_FixedSummary(),
                embedder_factory=self.embedders,
                logger=self.logger,
            )
            if hierarchical_services
            else None,
            search_factory=KnowledgeSearchFactory(tree_repository=self.tree, logger=self.logger)
            if hierarchical_services
            else None,
        )

    def warnings(self) -> list[str]:
        return [r.message for r in self.logger.records if r.level in ("warning", "error")]


def _agent_config(rag: RagConfig | None) -> AgentConfig:
    return AgentConfig(
        id="a1",
        nome="Agent",
        factory_ia_model="ollama",
        model="llama3.2:latest",
        descricao="Desc",
        prompt="Prompt",
        rag_config=rag,
    )


def _hierarchical(doc_name: str = "manual.md") -> RagConfig:
    return RagConfig(
        active=True,
        doc_name=doc_name,
        model="nomic-embed-text:latest",
        factory_ia_model="ollama",
        search_strategy=SearchStrategy.HIERARCHICAL,
    )


def _toolkit_names(agent: object) -> list[str]:
    return [tool.name for tool in getattr(agent, "tools", None) or [] if isinstance(tool, Toolkit)]


@pytest.fixture
def docs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A montagem lê ``docs/<doc_name>`` relativo ao cwd."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "manual.md").write_text("# Manual\n\nIntro.\n\n## Instalação\n\nPasso a passo.\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return docs


@pytest.mark.usefixtures("docs_dir")
async def test_estrategia_hierarquica_da_a_tool_de_busca_e_indexa_a_arvore_sem_knowledge_do_agno() -> None:
    assembly = _Assembly()

    agent = await assembly.service.create_agent(_agent_config(_hierarchical()))

    # Sem o Knowledge (RAG semântico) do agno nem as buscas que ele liga; a busca é a tool própria.
    assert agent.knowledge is None
    assert (agent.search_knowledge, agent.read_chat_history) == (False, False)
    assert _toolkit_names(agent) == ["hierarchical_search"]
    # O documento foi indexado na árvore (raiz e seção), com o embedder da fábrica.
    assert await assembly.tree.exists("manual.md") is True
    roots = await assembly.tree.get_root_nodes("manual.md")
    assert [node.title for node in roots] == ["Manual"]
    assert [node.title for node in await assembly.tree.get_children(roots[0].id)] == ["Instalação"]
    assert assembly.embedders.created and assembly.warnings() == []


@pytest.mark.parametrize("rag", [None, RagConfig(active=False)], ids=["sem-rag", "rag-inativo"])
@pytest.mark.usefixtures("docs_dir")
async def test_sem_rag_ou_com_rag_inativo_o_agente_sobe_sem_knowledge_sem_tool_e_sem_indexar(
    rag: RagConfig | None,
) -> None:
    assembly = _Assembly()

    agent = await assembly.service.create_agent(_agent_config(rag))

    assert agent.knowledge is None and agent.search_knowledge is False
    assert not agent.tools
    assert await assembly.tree.exists("manual.md") is False
    assert assembly.embedders.created == [] and assembly.warnings() == []


@pytest.mark.usefixtures("docs_dir")
async def test_fabrica_sem_indexacao_nem_busca_sobe_o_agente_hierarquico_sem_a_tool_e_avisa() -> None:
    """Os dois serviços são opcionais na fábrica (retrocompat): sem eles a estratégia fica sem tool."""
    assembly = _Assembly(hierarchical_services=False)

    agent = await assembly.service.create_agent(_agent_config(_hierarchical()))

    assert agent.id == "a1" and agent.knowledge is None
    assert not agent.tools
    assert assembly.warnings() == ["Indexing service ou search factory não disponíveis para estratégia HIERARCHICAL"]


@pytest.mark.usefixtures("docs_dir")
async def test_documento_hierarquico_que_nao_existe_sobe_o_agente_sem_a_tool_e_avisa() -> None:
    assembly = _Assembly()

    agent = await assembly.service.create_agent(_agent_config(_hierarchical("nao-existe.md")))

    assert not agent.tools
    assert await assembly.tree.exists("nao-existe.md") is False
    assert assembly.warnings() == ["Documento não encontrado"]
