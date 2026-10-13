"""F2-11 (d): a mesma garantia no RAG hierárquico, que grava o embedding de cada nó em ``document_tree``.

- (b) nó com embedding vazio, de tamanho diferente dos demais ou que falhou não é gravado: a árvore
  inteira fica de fora (gravar parte dela pularia a reindexação para sempre, pelo ``exists``), com
  log de contagem e ``EmbeddingRejectedError``; o agente sobe sem a tool hierárquica.
- (c) árvore gravada antes do F2-11 com ``embedding: []`` é refeita na próxima indexação.
- (a) com a sonda, o ``rag_config`` legado do Ollama (``OllamaEmbedder`` real do agno, 768 posições)
  grava vetores inteiros.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import pytest
from agno.knowledge.embedder.base import Embedder
from agno.knowledge.embedder.ollama import OllamaEmbedder

from src.application.services.document_indexing_service import DocumentIndexingService, EmbeddingRejectedError
from src.application.services.knowledge_search_factory import KnowledgeSearchFactory
from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.document_node import DocumentNode
from src.domain.entities.model_config import ModelConfig
from src.domain.entities.rag_config import RagConfig, SearchStrategy
from src.domain.ports.embedder_factory_port import IEmbedderFactory
from src.domain.ports.summary_generator_port import ISummaryGenerator
from src.infrastructure.parsers.text_document_parser import TextDocumentParser
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService
from src.infrastructure.runtime.agno.embedder_dimensions import DimensionResolvingEmbedderFactory
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from tests.fakes import (
    FakeEmbedderFactory,
    FakeModelFactory,
    InMemoryDocumentTreeRepository,
    InMemoryToolRepository,
    RecordingLogger,
)
from tests.fakes.embedders import FakeOllamaClient, ScriptedEmbedder
from tests.fakes.session_db import fake_session_db

DOC_MARKER = "MARCADOR-DO-TEXTO-DO-DOCUMENTO"
DOC = f"# T\n\nIntro {DOC_MARKER}.\n\n## A\n\nTexto A {DOC_MARKER}.\n\n## B\n\nTexto B {DOC_MARKER}.\n"
RAG = RagConfig(active=True, doc_name="m.md", search_strategy=SearchStrategy.HIERARCHICAL)


class _FixedSummary(ISummaryGenerator):
    async def generate_summary(self, content: str) -> str:
        return f"resumo de {content[:10]}"


class _Factory(IEmbedderFactory):
    def __init__(self, make: Callable[[ModelConfig], Embedder]) -> None:
        self._make = make

    def create_embedder(self, config: ModelConfig) -> Embedder:
        return self._make(config)


class _RaisingEmbedder(ScriptedEmbedder):
    def get_embedding(self, text: str) -> list[float]:
        if "Texto B" in text:
            raise ConnectionError(f"falhou com {DOC_MARKER}")
        return super().get_embedding(text)


def _scripted(size_for: Callable[[str], int | None]) -> _Factory:
    return _Factory(lambda _config: ScriptedEmbedder(dimensions=8, size_for=size_for))


def _service(
    tree: InMemoryDocumentTreeRepository, embedders: IEmbedderFactory, logger: RecordingLogger
) -> DocumentIndexingService:
    return DocumentIndexingService(
        parser=TextDocumentParser(),
        tree_repository=tree,
        summary_generator=_FixedSummary(),
        embedder_factory=embedders,
        logger=logger,
    )


def _stored(tree: InMemoryDocumentTreeRepository, doc_name: str = "m.md") -> list[DocumentNode]:
    return [n for n in tree._nodes if n.doc_name == doc_name]


def _no_document_text(logger: RecordingLogger) -> None:
    assert DOC_MARKER not in repr(logger.records)


# ── (b) ──────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("embedders", "rejected"),
    [
        pytest.param(_scripted(lambda _text: 0), 3, id="todos-vazios"),
        pytest.param(_scripted(lambda text: 4 if "Texto B" in text else None), 1, id="tamanho-diferente"),
        pytest.param(_Factory(lambda _config: _RaisingEmbedder(dimensions=8)), 1, id="embedder-falhou"),
    ],
)
async def test_no_com_embedding_invalido_nao_grava_a_arvore(embedders: IEmbedderFactory, rejected: int) -> None:
    tree = InMemoryDocumentTreeRepository()
    logger = RecordingLogger()

    with pytest.raises(EmbeddingRejectedError):
        await _service(tree, embedders, logger).index_document("m.md", DOC, RAG)

    assert _stored(tree) == []
    [record] = [r for r in logger.records if r.level == "error"]
    assert record.context == {
        "doc_name": "m.md",
        "rejected": rejected,
        "total": 3,
        "error_type": "EmbeddingRejectedError",
    }
    _no_document_text(logger)


async def test_depois_da_recusa_a_proxima_indexacao_grava_a_arvore() -> None:
    tree = InMemoryDocumentTreeRepository()
    with pytest.raises(EmbeddingRejectedError):
        await _service(tree, _scripted(lambda _text: 0), RecordingLogger()).index_document("m.md", DOC, RAG)

    nodes = await _service(tree, FakeEmbedderFactory(dimensions=8), RecordingLogger()).index_document("m.md", DOC, RAG)

    assert len(nodes) == 3
    assert [len(n.embedding or []) for n in _stored(tree)] == [8, 8, 8]


async def test_com_a_sonda_o_legado_do_ollama_grava_vetores_de_768() -> None:
    tree = InMemoryDocumentTreeRepository()
    logger = RecordingLogger()
    client = FakeOllamaClient(native_size=768)
    ollama = _Factory(lambda config: OllamaEmbedder(id=config.model_id, ollama_client=client))  # type: ignore[arg-type]

    await _service(tree, DimensionResolvingEmbedderFactory(ollama, logger=logger), logger).index_document(
        "m.md", DOC, RAG
    )

    assert [len(n.embedding or []) for n in _stored(tree)] == [768, 768, 768]


# ── (c) ──────────────────────────────────────────────────────────────


async def _legacy_tree(tree: InMemoryDocumentTreeRepository) -> None:
    """O que a indexação de antes do F2-11 gravava com o Ollama legado: ``[]`` em todo nó."""
    nodes = TextDocumentParser().parse(DOC, "m.md")
    for node in nodes:
        node.embedding = []
    await tree.save_nodes(nodes)
    other = DocumentNode(id="outro::0", doc_name="outro.md", level=0, title="O", content="o", embedding=[])
    await tree.save_nodes([other])


async def test_arvore_legada_com_embedding_vazio_e_refeita() -> None:
    tree = InMemoryDocumentTreeRepository()
    await _legacy_tree(tree)
    logger = RecordingLogger()

    nodes = await _service(tree, FakeEmbedderFactory(dimensions=8), logger).index_document("m.md", DOC, RAG)

    assert len(nodes) == 3
    assert [len(n.embedding or []) for n in _stored(tree)] == [8, 8, 8]
    assert [n.embedding for n in _stored(tree, "outro.md")] == [[]]  # outro documento: não é tocado
    warnings = [(r.message, r.context) for r in logger.records if r.level == "warning"]
    assert ("Árvore do documento com embedding vazio (gravada antes do F2-11); reindexando", {"doc_name": "m.md"}) in (
        warnings
    )
    assert any(r.context == {"doc_name": "m.md", "deleted": 3} for r in logger.records if r.level == "info")


async def test_arvore_refeita_nao_e_refeita_de_novo() -> None:
    tree = InMemoryDocumentTreeRepository()
    await _legacy_tree(tree)
    service = _service(tree, FakeEmbedderFactory(dimensions=8), RecordingLogger())
    await service.index_document("m.md", DOC, RAG)
    before = [(n.id, n.embedding) for n in _stored(tree)]

    assert await service.index_document("m.md", DOC, RAG) == []
    assert [(n.id, n.embedding) for n in _stored(tree)] == before


async def test_arvore_legada_continua_la_se_a_reindexacao_e_recusada() -> None:
    """Nada é apagado antes de a árvore nova estar pronta (o próximo startup tenta de novo)."""
    tree = InMemoryDocumentTreeRepository()
    await _legacy_tree(tree)

    with pytest.raises(EmbeddingRejectedError):
        await _service(tree, _scripted(lambda _text: 0), RecordingLogger()).index_document("m.md", DOC, RAG)

    assert [n.embedding for n in _stored(tree)] == [[], [], []]


# ── agente ───────────────────────────────────────────────────────────


async def test_agente_sobe_sem_a_tool_hierarquica_quando_o_embedding_e_recusado(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "m.md").write_text(DOC, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    logger = RecordingLogger()
    tree = InMemoryDocumentTreeRepository()
    embedders = _scripted(lambda _text: 0)
    factory = AgentFactoryService(
        db_url="mongodb://mongo.test.invalid:27017",
        session_db=fake_session_db(),
        logger=logger,
        model_factory=FakeModelFactory(),
        embedder_factory=embedders,
        tool_factory=HttpToolFactory(logger=logger),
        tool_repository=InMemoryToolRepository(),
        indexing_service=_service(tree, embedders, logger),
        search_factory=KnowledgeSearchFactory(tree_repository=tree, logger=logger),
    )
    config = AgentConfig(
        id="h", nome="H", factory_ia_model="ollama", model="llama3.2:latest", descricao="d", prompt="p", rag_config=RAG
    )

    agent = await factory.create_agent(config)

    assert not agent.tools
    assert _stored(tree) == []
    assert [r.context["error_type"] for r in logger.records if r.level == "warning" and "error_type" in r.context] == [
        "EmbeddingRejectedError"
    ]
    _no_document_text(logger)
