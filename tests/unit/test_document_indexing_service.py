"""Testes unitários para DocumentIndexingService."""

from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock

import pytest

from src.application.services.document_indexing_service import DocumentIndexingService
from src.domain.entities.document_node import DocumentNode
from src.domain.entities.rag_config import RagConfig, SearchStrategy
from src.domain.ports.summary_generator_port import SummaryTimeoutError
from src.infrastructure.parsers.text_document_parser import TextDocumentParser
from tests.fakes import FakeEmbedderFactory, InMemoryDocumentTreeRepository, RecordingLogger


def _make_nodes(doc_name: str = "test.txt") -> list[DocumentNode]:
    parent = DocumentNode(
        id=f"{doc_name}::node::0",
        doc_name=doc_name,
        level=0,
        title="Parent",
        content="Parent content",
        children_ids=[f"{doc_name}::node::1"],
    )
    child = DocumentNode(
        id=f"{doc_name}::node::1",
        doc_name=doc_name,
        level=1,
        title="Child",
        content="Child content",
        parent_id=f"{doc_name}::node::0",
    )
    return [parent, child]


class TestDocumentIndexingService:
    """Testes do serviço de indexação hierárquica."""

    def setup_method(self):
        self.mock_parser = MagicMock()
        self.mock_tree_repo = AsyncMock()
        self.mock_summary_gen = AsyncMock()
        self.mock_embedder_factory = MagicMock()
        self.mock_logger = MagicMock()

        self.service = DocumentIndexingService(
            parser=self.mock_parser,
            tree_repository=self.mock_tree_repo,
            summary_generator=self.mock_summary_gen,
            embedder_factory=self.mock_embedder_factory,
            logger=self.mock_logger,
        )

    @pytest.mark.asyncio
    async def test_skip_if_already_indexed(self):
        self.mock_tree_repo.exists.return_value = True
        rag = RagConfig(active=True, doc_name="test.txt")

        result = await self.service.index_document("test.txt", "content", rag)

        assert result == []
        self.mock_parser.parse.assert_not_called()
        self.mock_tree_repo.save_nodes.assert_not_called()

    @pytest.mark.asyncio
    async def test_index_document_full_flow(self):
        self.mock_tree_repo.exists.return_value = False
        nodes = _make_nodes()
        self.mock_parser.parse.return_value = nodes
        self.mock_summary_gen.generate_summary.return_value = "Resumo gerado"

        mock_embedder = MagicMock()
        mock_embedder.get_embedding.return_value = [0.1, 0.2, 0.3]
        self.mock_embedder_factory.create_model.return_value = mock_embedder

        rag = RagConfig(
            active=True,
            doc_name="test.txt",
            model="nomic-embed-text:latest",
            factory_ia_model="ollama",
            search_strategy=SearchStrategy.HIERARCHICAL,
        )

        result = await self.service.index_document("test.txt", "# Title\nContent", rag)

        assert len(result) == 2
        self.mock_tree_repo.save_nodes.assert_called_once_with(nodes)
        # Parent node should get summary
        self.mock_summary_gen.generate_summary.assert_called()

    @pytest.mark.asyncio
    async def test_empty_parse_returns_empty(self):
        self.mock_tree_repo.exists.return_value = False
        self.mock_parser.parse.return_value = []
        rag = RagConfig(active=True, doc_name="test.txt")

        result = await self.service.index_document("test.txt", "content", rag)
        assert result == []

    @pytest.mark.asyncio
    async def test_summary_fallback_on_error(self):
        self.mock_tree_repo.exists.return_value = False
        nodes = _make_nodes()
        self.mock_parser.parse.return_value = nodes
        self.mock_summary_gen.generate_summary.side_effect = Exception("LLM down")

        mock_embedder = MagicMock()
        mock_embedder.get_embedding.return_value = [0.1]
        self.mock_embedder_factory.create_model.return_value = mock_embedder

        rag = RagConfig(
            active=True,
            doc_name="test.txt",
            model="m",
            factory_ia_model="ollama",
        )
        result = await self.service.index_document("test.txt", "content", rag)

        # Parent should have fallback summary (first 200 chars)
        parent = [n for n in result if not n.is_leaf][0]
        assert parent.summary == parent.content[:200]

    @pytest.mark.asyncio
    async def test_embedding_failure_logged(self):
        self.mock_tree_repo.exists.return_value = False
        nodes = [
            DocumentNode(
                id="n0", doc_name="t.txt", level=0, title="T", content="Content"
            ),
        ]
        self.mock_parser.parse.return_value = nodes
        self.mock_summary_gen.generate_summary.return_value = "Sum"

        mock_embedder = MagicMock()
        mock_embedder.get_embedding.side_effect = Exception("emb fail")
        self.mock_embedder_factory.create_model.return_value = mock_embedder

        rag = RagConfig(active=True, doc_name="t.txt", model="m", factory_ia_model="o")
        result = await self.service.index_document("t.txt", "content", rag)

        assert len(result) == 1
        assert result[0].embedding is None
        self.mock_logger.warning.assert_called()


async def test_embeddings_da_indexacao_sao_calculados_fora_do_event_loop():
    """F1-07: ``get_embedding`` é síncrono (rede no provider real); não pode travar o loop."""
    tree_repo = InMemoryDocumentTreeRepository()
    embedder_factory = FakeEmbedderFactory(dimensions=4)
    summary = AsyncMock()
    summary.generate_summary.return_value = "resumo"
    service = DocumentIndexingService(
        parser=TextDocumentParser(),
        tree_repository=tree_repo,
        summary_generator=summary,
        embedder_factory=embedder_factory,
        logger=RecordingLogger(),
    )
    rag = RagConfig(active=True, doc_name="m.md", search_strategy=SearchStrategy.HIERARCHICAL)

    nodes = await service.index_document("m.md", "# T\n\nIntro.\n\n## A\n\nTexto A.\n\n## B\n\nTexto B.\n", rag)

    assert nodes and all(n.embedding for n in nodes)
    [embedder] = embedder_factory.embedders
    assert embedder.on_event_loop and not any(embedder.on_event_loop)


def _indexing_service(tree_repo, summary, logger=None) -> DocumentIndexingService:
    return DocumentIndexingService(
        parser=TextDocumentParser(),
        tree_repository=tree_repo,
        summary_generator=summary,
        embedder_factory=FakeEmbedderFactory(dimensions=4),
        logger=logger or RecordingLogger(),
    )


_DOC = "# T\n\nIntro.\n\n## A\n\nTexto A.\n\n## B\n\nTexto B.\n"


async def test_indexacao_concorrente_do_mesmo_documento_acontece_uma_vez():
    """BUG-F1-07-QA-1: o startup cria agentes com gather; o mesmo doc_name não pode ser indexado 2x."""
    tree_repo = InMemoryDocumentTreeRepository()
    summary = AsyncMock()
    summary.generate_summary.return_value = "resumo"
    logger = RecordingLogger()
    service = _indexing_service(tree_repo, summary, logger)
    rag = RagConfig(active=True, doc_name="m.md", search_strategy=SearchStrategy.HIERARCHICAL)

    first, second = await asyncio.gather(
        service.index_document("m.md", _DOC, rag),
        service.index_document("m.md", _DOC, rag),
    )

    assert len(first) == len(tree_repo._nodes) > 0
    assert second == []
    assert logger.messages("info").count("Iniciando indexação hierárquica") == 1
    assert "Indexação do documento em andamento; aguardando" in logger.messages("debug")


async def test_documentos_diferentes_indexam_em_paralelo():
    """Single-flight é por doc_name: um documento não espera o outro."""
    started: list[str] = []
    both_started = asyncio.Event()

    class _WaitForBoth:
        async def generate_summary(self, content: str) -> str:
            started.append(content)
            if len(started) >= 2:
                both_started.set()
            await asyncio.wait_for(both_started.wait(), timeout=5)
            return "resumo"

    tree_repo = InMemoryDocumentTreeRepository()
    service = _indexing_service(tree_repo, _WaitForBoth())
    rag = RagConfig(active=True, search_strategy=SearchStrategy.HIERARCHICAL)

    a, b = await asyncio.gather(
        service.index_document("a.md", _DOC, rag),
        service.index_document("b.md", _DOC, rag),
    )

    assert a and b
    assert {n.doc_name for n in tree_repo._nodes} == {"a.md", "b.md"}


_LONG_DOC = "# T\n\nIntro.\n\n" + "".join(f"## S{i}\n\nTexto {i}.\n\n### Sub{i}\n\nMais {i}.\n\n" for i in range(8))


class _TimeoutOnceGenerator:
    """Primeira chamada estoura o prazo; registra o conteúdo de cada chamada."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def generate_summary(self, content: str) -> str:
        self.calls.append(content)
        if len(self.calls) == 1:
            raise SummaryTimeoutError("modelo de sumário sem resposta")
        return "resumo"


async def test_apos_timeout_os_demais_nos_do_documento_vao_direto_ao_fallback():
    """N5: um modelo travado custaria um timeout por nó interno no startup."""
    tree_repo = InMemoryDocumentTreeRepository()
    generator = _TimeoutOnceGenerator()
    logger = RecordingLogger()
    service = _indexing_service(tree_repo, generator, logger)
    rag = RagConfig(active=True, search_strategy=SearchStrategy.HIERARCHICAL)

    nodes = await service.index_document("longo.md", _LONG_DOC, rag)

    internal = [n for n in nodes if not n.is_leaf]
    assert len(internal) > 5  # mais de um batch
    assert len(generator.calls) == 1
    assert all(n.summary == n.content[:200] for n in internal)
    warnings = [r for r in logger.records if r.level == "warning"]
    assert [r.message for r in warnings] == [
        "Modelo de sumário sem resposta; demais nós do documento usam truncamento"
    ]
    assert warnings[0].context == {"doc_name": "longo.md", "error_type": "SummaryTimeoutError"}

    # o corte vale só para aquele documento
    await service.index_document("outro.md", _DOC, rag)
    assert len(generator.calls) > 1
    assert all(n.summary == "resumo" for n in tree_repo._nodes if n.doc_name == "outro.md" and not n.is_leaf)


async def test_fallback_de_erro_comum_loga_so_o_tipo():
    tree_repo = InMemoryDocumentTreeRepository()
    summary = AsyncMock()
    summary.generate_summary.side_effect = RuntimeError("api_key=sk-teste-nao-logar")
    logger = RecordingLogger()
    service = _indexing_service(tree_repo, summary, logger)

    await service.index_document("m.md", _LONG_DOC, RagConfig(active=True))

    assert summary.generate_summary.await_count > 1  # erro comum não corta o documento
    assert {r.context.get("error_type") for r in logger.records if r.level == "warning"} == {"RuntimeError"}
    assert "sk-teste-nao-logar" not in repr(logger.records)
