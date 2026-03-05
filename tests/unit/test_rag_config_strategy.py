"""Testes unitários para RagConfig + SearchStrategy."""

from __future__ import annotations

from src.domain.entities.rag_config import RagConfig, SearchStrategy


class TestSearchStrategy:
    """Testes do enum SearchStrategy."""

    def test_semantic_value(self):
        assert SearchStrategy.SEMANTIC.value == "semantic"

    def test_hierarchical_value(self):
        assert SearchStrategy.HIERARCHICAL.value == "hierarchical"

    def test_from_string_semantic(self):
        assert SearchStrategy("semantic") == SearchStrategy.SEMANTIC

    def test_from_string_hierarchical(self):
        assert SearchStrategy("hierarchical") == SearchStrategy.HIERARCHICAL


class TestRagConfigSearchStrategy:
    """Testes de retrocompatibilidade do RagConfig."""

    def test_default_strategy_is_semantic(self):
        config = RagConfig(active=True, doc_name="doc.txt")
        assert config.search_strategy == SearchStrategy.SEMANTIC

    def test_explicit_hierarchical(self):
        config = RagConfig(
            active=True,
            doc_name="doc.txt",
            model="nomic-embed-text:latest",
            factory_ia_model="ollama",
            search_strategy=SearchStrategy.HIERARCHICAL,
        )
        assert config.search_strategy == SearchStrategy.HIERARCHICAL

    def test_retrocompat_no_strategy(self):
        """RagConfig sem search_strategy deve defaultar para SEMANTIC."""
        config = RagConfig(active=True)
        assert config.search_strategy == SearchStrategy.SEMANTIC
