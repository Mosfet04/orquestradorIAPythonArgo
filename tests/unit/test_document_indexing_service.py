"""Testes unitários para DocumentIndexingService."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.application.services.document_indexing_service import DocumentIndexingService
from src.domain.entities.document_node import DocumentNode
from src.domain.entities.rag_config import RagConfig, SearchStrategy


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
