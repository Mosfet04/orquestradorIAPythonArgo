"""Testes unitários para DocumentNode."""

from __future__ import annotations

import pytest

from src.domain.entities.document_node import DocumentNode


class TestDocumentNode:
    """Testes da entidade DocumentNode."""

    def test_create_valid_node(self):
        node = DocumentNode(
            id="doc::node::0",
            doc_name="test.txt",
            level=0,
            title="Introdução",
            content="Conteúdo de teste",
        )
        assert node.id == "doc::node::0"
        assert node.doc_name == "test.txt"
        assert node.level == 0
        assert node.title == "Introdução"
        assert node.content == "Conteúdo de teste"
        assert node.parent_id is None
        assert node.summary is None
        assert node.embedding is None
        assert node.children_ids == []

    def test_create_node_with_children(self):
        node = DocumentNode(
            id="doc::node::0",
            doc_name="test.txt",
            level=0,
            title="Root",
            content="Root content",
            children_ids=["doc::node::1", "doc::node::2"],
        )
        assert node.children_ids == ["doc::node::1", "doc::node::2"]
        assert not node.is_leaf

    def test_is_leaf_true(self):
        node = DocumentNode(
            id="doc::node::0",
            doc_name="test.txt",
            level=0,
            title="Leaf",
            content="Leaf content",
        )
        assert node.is_leaf

    def test_is_leaf_false(self):
        node = DocumentNode(
            id="doc::node::0",
            doc_name="test.txt",
            level=0,
            title="Parent",
            content="Parent content",
            children_ids=["child1"],
        )
        assert not node.is_leaf

    def test_searchable_text_returns_summary(self):
        node = DocumentNode(
            id="doc::node::0",
            doc_name="test.txt",
            level=0,
            title="Title",
            content="Full content here",
            summary="Resumo conciso",
        )
        assert node.searchable_text == "Resumo conciso"

    def test_searchable_text_returns_content_when_no_summary(self):
        node = DocumentNode(
            id="doc::node::0",
            doc_name="test.txt",
            level=0,
            title="Title",
            content="Full content here",
        )
        assert node.searchable_text == "Full content here"

    def test_empty_id_raises(self):
        with pytest.raises(ValueError, match="ID do nó"):
            DocumentNode(
                id="",
                doc_name="test.txt",
                level=0,
                title="Title",
                content="Content",
            )

    def test_empty_doc_name_raises(self):
        with pytest.raises(ValueError, match="doc_name"):
            DocumentNode(
                id="node1",
                doc_name="",
                level=0,
                title="Title",
                content="Content",
            )

    def test_negative_level_raises(self):
        with pytest.raises(ValueError, match="level"):
            DocumentNode(
                id="node1",
                doc_name="test.txt",
                level=-1,
                title="Title",
                content="Content",
            )

    def test_empty_title_raises(self):
        with pytest.raises(ValueError, match="title"):
            DocumentNode(
                id="node1",
                doc_name="test.txt",
                level=0,
                title="",
                content="Content",
            )

    def test_node_with_embedding(self):
        emb = [0.1, 0.2, 0.3]
        node = DocumentNode(
            id="node1",
            doc_name="test.txt",
            level=0,
            title="Title",
            content="Content",
            embedding=emb,
        )
        assert node.embedding == [0.1, 0.2, 0.3]
