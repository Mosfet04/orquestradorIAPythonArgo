"""Testes unitários para KnowledgeSearchFactory."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.application.services.knowledge_search_factory import KnowledgeSearchFactory
from src.application.services.search_strategies.hierarchical_search_strategy import (
    HierarchicalSearchStrategy,
)
from src.application.services.search_strategies.semantic_search_strategy import (
    SemanticSearchStrategy,
)
from src.domain.entities.rag_config import RagConfig, SearchStrategy


class TestKnowledgeSearchFactory:
    """Testes da factory de estratégias de busca."""

    def setup_method(self):
        self.mock_tree_repo = AsyncMock()
        self.mock_logger = MagicMock()
        self.factory = KnowledgeSearchFactory(
            tree_repository=self.mock_tree_repo,
            logger=self.mock_logger,
        )

    def test_create_semantic_strategy(self):
        config = RagConfig(
            active=True,
            search_strategy=SearchStrategy.SEMANTIC,
        )
        mock_knowledge = MagicMock()

        strategy = self.factory.create_strategy(config, knowledge=mock_knowledge)
        assert isinstance(strategy, SemanticSearchStrategy)

    def test_create_hierarchical_strategy(self):
        config = RagConfig(
            active=True,
            doc_name="test.txt",
            search_strategy=SearchStrategy.HIERARCHICAL,
        )
        mock_embedder = MagicMock()

        strategy = self.factory.create_strategy(config, embedder=mock_embedder)
        assert isinstance(strategy, HierarchicalSearchStrategy)

    def test_semantic_without_knowledge_raises(self):
        config = RagConfig(
            active=True,
            search_strategy=SearchStrategy.SEMANTIC,
        )
        with pytest.raises(ValueError, match="knowledge"):
            self.factory.create_strategy(config)

    def test_hierarchical_without_embedder_raises(self):
        config = RagConfig(
            active=True,
            doc_name="test.txt",
            search_strategy=SearchStrategy.HIERARCHICAL,
        )
        with pytest.raises(ValueError, match="embedder"):
            self.factory.create_strategy(config)

    def test_hierarchical_without_doc_name_raises(self):
        config = RagConfig(
            active=True,
            search_strategy=SearchStrategy.HIERARCHICAL,
        )
        with pytest.raises(ValueError, match="doc_name"):
            self.factory.create_strategy(config, embedder=MagicMock())

    def test_default_strategy_is_semantic(self):
        config = RagConfig(active=True)
        mock_knowledge = MagicMock()

        strategy = self.factory.create_strategy(config, knowledge=mock_knowledge)
        assert isinstance(strategy, SemanticSearchStrategy)
