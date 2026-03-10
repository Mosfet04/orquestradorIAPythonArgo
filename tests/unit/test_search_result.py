"""Testes unitários para SearchResult."""

from __future__ import annotations

import pytest

from src.domain.entities.search_result import SearchResult


class TestSearchResult:
    """Testes da entidade SearchResult."""

    def test_create_valid_result(self):
        result = SearchResult(
            content="Conteúdo relevante",
            score=0.95,
            node_id="node1",
            metadata={"strategy": "semantic"},
        )
        assert result.content == "Conteúdo relevante"
        assert result.score == 0.95
        assert result.node_id == "node1"
        assert result.metadata == {"strategy": "semantic"}

    def test_default_values(self):
        result = SearchResult(content="Content", score=0.5)
        assert result.node_id == ""
        assert result.metadata == {}

    def test_empty_content_raises(self):
        with pytest.raises(ValueError, match="content"):
            SearchResult(content="", score=0.5)

    def test_score_below_zero_raises(self):
        with pytest.raises(ValueError, match="score"):
            SearchResult(content="Content", score=-0.1)

    def test_score_above_one_raises(self):
        with pytest.raises(ValueError, match="score"):
            SearchResult(content="Content", score=1.1)

    def test_score_zero_valid(self):
        result = SearchResult(content="Content", score=0.0)
        assert result.score == 0.0

    def test_score_one_valid(self):
        result = SearchResult(content="Content", score=1.0)
        assert result.score == 1.0
