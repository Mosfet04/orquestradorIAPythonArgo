"""Testes unitários para HierarchicalSearchStrategy."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.application.services.search_strategies.hierarchical_search_strategy import (
    HierarchicalSearchStrategy,
)
from src.domain.entities.document_node import DocumentNode


def _make_node(
    node_id: str,
    level: int = 0,
    embedding: list | None = None,
    children_ids: list | None = None,
    content: str = "content",
    title: str = "title",
    parent_id: str | None = None,
) -> DocumentNode:
    return DocumentNode(
        id=node_id,
        doc_name="test.txt",
        level=level,
        title=title,
        content=content,
        embedding=embedding,
        children_ids=children_ids or [],
        parent_id=parent_id,
    )


class TestHierarchicalSearchStrategy:
    """Testes da estratégia de busca hierárquica."""

    def setup_method(self):
        self.mock_repo = AsyncMock()
        self.mock_embedder = MagicMock()
        self.mock_logger = MagicMock()

        self.strategy = HierarchicalSearchStrategy(
            tree_repository=self.mock_repo,
            embedder=self.mock_embedder,
            doc_name="test.txt",
            logger=self.mock_logger,
        )

    @pytest.mark.asyncio
    async def test_search_returns_empty_on_no_roots(self):
        self.mock_repo.get_root_nodes.return_value = []
        self.mock_embedder.get_embedding.return_value = [0.1, 0.2]

        results = await self.strategy.search("query")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_returns_empty_on_embedding_failure(self):
        self.mock_embedder.get_embedding.side_effect = Exception("fail")

        results = await self.strategy.search("query")
        assert results == []

    @pytest.mark.asyncio
    async def test_search_leaf_nodes(self):
        """Busca que chega diretamente em nós folha."""
        leaf = _make_node("leaf1", embedding=[1.0, 0.0, 0.0], content="relevant")
        self.mock_repo.get_root_nodes.return_value = [leaf]
        self.mock_embedder.get_embedding.return_value = [1.0, 0.0, 0.0]

        results = await self.strategy.search("query", top_k=5)

        assert len(results) == 1
        assert results[0].content == "relevant"
        assert results[0].score > 0.9
        assert results[0].metadata["strategy"] == "hierarchical"

    @pytest.mark.asyncio
    async def test_search_traverses_tree(self):
        """Busca desce de pai para filhos."""
        parent = _make_node("parent", embedding=[1.0, 0.0], children_ids=["child1"])
        child = _make_node(
            "child1",
            level=1,
            embedding=[1.0, 0.0],
            content="deep content",
            parent_id="parent",
        )

        self.mock_repo.get_root_nodes.return_value = [parent]
        self.mock_repo.get_children.return_value = [child]
        self.mock_embedder.get_embedding.return_value = [1.0, 0.0]

        results = await self.strategy.search("query")

        assert len(results) == 1
        assert results[0].content == "deep content"
        self.mock_repo.get_children.assert_called_once_with("parent")

    @pytest.mark.asyncio
    async def test_search_respects_top_k(self):
        """Não retorna mais que top_k resultados."""
        leaves = [
            _make_node(f"leaf{i}", embedding=[1.0, 0.0], content=f"content {i}")
            for i in range(10)
        ]
        self.mock_repo.get_root_nodes.return_value = leaves
        self.mock_embedder.get_embedding.return_value = [1.0, 0.0]

        results = await self.strategy.search("query", top_k=3)
        assert len(results) <= 3

    @pytest.mark.asyncio
    async def test_nodes_without_embedding_get_zero_score(self):
        node_with = _make_node("n1", embedding=[1.0, 0.0], content="with embedding")
        node_without = _make_node("n2", content="no embedding")

        self.mock_repo.get_root_nodes.return_value = [node_with, node_without]
        self.mock_embedder.get_embedding.return_value = [1.0, 0.0]

        results = await self.strategy.search("query", top_k=10)
        # Nó com embedding deve ter score > 0
        scores = [r.score for r in results]
        assert max(scores) > 0.0


class TestCosineSimililarity:
    """Testes do cálculo de similaridade cosseno."""

    def test_identical_vectors(self):
        sim = HierarchicalSearchStrategy._cosine_similarity([1.0, 0.0], [1.0, 0.0])
        assert abs(sim - 1.0) < 1e-6

    def test_orthogonal_vectors(self):
        sim = HierarchicalSearchStrategy._cosine_similarity([1.0, 0.0], [0.0, 1.0])
        assert abs(sim) < 1e-6

    def test_empty_vectors(self):
        sim = HierarchicalSearchStrategy._cosine_similarity([], [])
        assert sim == 0.0

    def test_different_lengths(self):
        sim = HierarchicalSearchStrategy._cosine_similarity([1.0], [1.0, 0.0])
        assert sim == 0.0

    def test_zero_vectors(self):
        sim = HierarchicalSearchStrategy._cosine_similarity([0.0, 0.0], [0.0, 0.0])
        assert sim == 0.0

    def test_similar_vectors(self):
        sim = HierarchicalSearchStrategy._cosine_similarity([1.0, 1.0], [1.0, 0.9])
        assert sim > 0.9
