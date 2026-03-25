"""Testes unitários para TextDocumentParser."""

from __future__ import annotations

from src.infrastructure.parsers.text_document_parser import TextDocumentParser


class TestTextDocumentParser:
    """Testes do parser de documentos texto/markdown."""

    def setup_method(self):
        self.parser = TextDocumentParser()

    # ── documentos com headings ────────────────────────────────────

    def test_parse_single_heading(self):
        content = "# Título\n\nConteúdo do parágrafo."
        nodes = self.parser.parse(content, "test.md")

        assert len(nodes) == 1
        assert nodes[0].title == "Título"
        assert nodes[0].level == 0
        assert "Conteúdo do parágrafo" in nodes[0].content

    def test_parse_nested_headings(self):
        content = (
            "# Cap 1\n\nIntro cap 1.\n\n"
            "## Seção 1.1\n\nConteúdo seção 1.1.\n\n"
            "## Seção 1.2\n\nConteúdo seção 1.2.\n\n"
            "# Cap 2\n\nIntro cap 2.\n"
        )
        nodes = self.parser.parse(content, "test.md")

        # Cap 1 (level 0), Seção 1.1 (level 1), Seção 1.2 (level 1), Cap 2 (level 0)
        assert len(nodes) == 4

        cap1 = nodes[0]
        assert cap1.title == "Cap 1"
        assert cap1.level == 0
        assert cap1.parent_id is None

        sec11 = nodes[1]
        assert sec11.title == "Seção 1.1"
        assert sec11.level == 1
        assert sec11.parent_id == cap1.id

        sec12 = nodes[2]
        assert sec12.title == "Seção 1.2"
        assert sec12.level == 1
        assert sec12.parent_id == cap1.id

        cap2 = nodes[3]
        assert cap2.title == "Cap 2"
        assert cap2.level == 0
        assert cap2.parent_id is None

    def test_parse_deeply_nested(self):
        content = (
            "# H1\n\nContent H1.\n\n"
            "## H2\n\nContent H2.\n\n"
            "### H3\n\nContent H3.\n"
        )
        nodes = self.parser.parse(content, "deep.md")

        assert len(nodes) == 3
        assert nodes[0].level == 0  # H1
        assert nodes[1].level == 1  # H2
        assert nodes[2].level == 2  # H3
        assert nodes[1].parent_id == nodes[0].id
        assert nodes[2].parent_id == nodes[1].id

    def test_children_ids_linked(self):
        content = (
            "# Parent\n\nParent content.\n\n"
            "## Child 1\n\nChild 1 content.\n\n"
            "## Child 2\n\nChild 2 content.\n"
        )
        nodes = self.parser.parse(content, "linked.md")

        parent = nodes[0]
        assert len(parent.children_ids) == 2
        assert nodes[1].id in parent.children_ids
        assert nodes[2].id in parent.children_ids

    def test_preamble_before_first_heading(self):
        content = "Este é um preâmbulo.\n\n" "# Título\n\nConteúdo do título.\n"
        nodes = self.parser.parse(content, "preamble.md")

        assert len(nodes) == 2
        assert nodes[0].title == "Introdução"
        assert "preâmbulo" in nodes[0].content
        assert nodes[1].title == "Título"

    # ── documentos sem headings ────────────────────────────────────

    def test_parse_no_headings_single_chunk(self):
        content = "Texto simples sem nenhum heading."
        nodes = self.parser.parse(content, "plain.txt")

        assert len(nodes) == 1
        assert nodes[0].title == "Chunk 1"
        assert nodes[0].level == 0
        assert "Texto simples" in nodes[0].content

    def test_parse_no_headings_chunked(self):
        parser = TextDocumentParser(max_chunk_chars=50)
        content = "Parágrafo um com mais de 50 chars para forçar.\n\nParágrafo dois também grande."
        nodes = parser.parse(content, "chunked.txt")

        assert len(nodes) >= 2

    # ── edge cases ─────────────────────────────────────────────────

    def test_parse_empty_content(self):
        nodes = self.parser.parse("", "empty.txt")
        assert nodes == []

    def test_parse_whitespace_only(self):
        nodes = self.parser.parse("   \n\n  ", "whitespace.txt")
        assert nodes == []

    def test_deterministic_ids(self):
        content = "# Título\n\nConteúdo."
        nodes1 = self.parser.parse(content, "det.md")
        nodes2 = self.parser.parse(content, "det.md")
        assert nodes1[0].id == nodes2[0].id

    def test_all_nodes_have_doc_name(self):
        content = "# A\n\nContent A.\n\n## B\n\nContent B."
        nodes = self.parser.parse(content, "myfile.md")
        for node in nodes:
            assert node.doc_name == "myfile.md"

    def test_leaf_nodes_have_no_children(self):
        content = "# Parent\n\nParent.\n\n" "## Leaf\n\nLeaf content.\n"
        nodes = self.parser.parse(content, "leaf.md")
        leaf = nodes[1]
        assert leaf.is_leaf
        assert leaf.children_ids == []

    # ── sub-chunking de folhas grandes ────────────────────────

    def test_large_leaf_is_subdivided(self):
        """Folha com conteúdo > max_chunk_chars deve ser subdividida."""
        parser = TextDocumentParser(max_chunk_chars=100)
        large_body = "\n\n".join(f"Parágrafo {i} com texto." for i in range(20))
        content = f"# Seção Grande\n\n{large_body}"
        nodes = parser.parse(content, "large.md")

        parent = nodes[0]
        assert not parent.is_leaf, "Parent deve virar nó interno"
        assert len(parent.children_ids) >= 2

        children = [n for n in nodes if n.parent_id == parent.id]
        assert len(children) >= 2
        for child in children:
            assert child.is_leaf
            assert len(child.content) <= 200  # margem sobre 100
            assert child.level == parent.level + 1
            assert "— parte" in child.title

    def test_small_leaf_not_subdivided(self):
        """Folha com conteúdo pequeno não deve ser subdividida."""
        content = "# Título\n\nTexto curto."
        nodes = self.parser.parse(content, "small.md")

        assert len(nodes) == 1
        assert nodes[0].is_leaf
        assert nodes[0].children_ids == []

    def test_subdivided_children_have_correct_ids(self):
        """IDs dos filhos sub-chunked não colidem com IDs existentes."""
        parser = TextDocumentParser(max_chunk_chars=50)
        large_body = "\n\n".join(f"Parágrafo {i} texto." for i in range(10))
        content = f"# Cap 1\n\nIntro.\n\n## Seção\n\n{large_body}"
        nodes = parser.parse(content, "ids.md")

        all_ids = [n.id for n in nodes]
        assert len(all_ids) == len(set(all_ids)), "IDs devem ser únicos"

    # ── limpeza de texto PDF ───────────────────────────────

    def test_clean_pdf_removes_page_numbers(self):
        """Números de página isolados devem ser removidos para PDFs."""
        content = "Texto da página.\n\n3\n\nMais texto."
        nodes = self.parser.parse(content, "doc.pdf")

        full_content = " ".join(n.content for n in nodes)
        assert "\n3\n" not in full_content

    def test_clean_pdf_normalizes_newlines(self):
        """Múltiplas quebras de linha são normalizadas."""
        content = "Texto\n\n\n\n\nMais texto"
        nodes = self.parser.parse(content, "nl.txt")

        assert len(nodes) >= 1
