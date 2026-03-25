"""Parser de documentos texto/markdown em árvore hierárquica."""

from __future__ import annotations

import re
from typing import List, Optional, Tuple

from src.domain.entities.document_node import DocumentNode
from src.domain.ports.document_parser_port import IDocumentParser

_MARKDOWN_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$")
_NUMBERED_HEADING_RE = re.compile(r"^(\d+(?:\.\d+)*)(?:\.)?\s+(.+)$")
_UNIT_HEADING_RE = re.compile(r"^Unidade\s+(\d+)\s*[-–—]\s*(.+)$", re.IGNORECASE)
_PAGE_NUMBER_RE = re.compile(r"^\d{1,4}$")
_MAX_CHUNK_CHARS = 1200


class TextDocumentParser(IDocumentParser):
    """Parseia documentos ``.txt`` e ``.md`` em nós hierárquicos.

    Detecta headings por regex (``# ``, ``## ``, ``### ``, …) e cria
    uma árvore onde cada heading é um nó interno e o conteúdo entre
    headings são nós folha.

    Documentos sem headings são divididos em chunks por tamanho.
    """

    def __init__(self, *, max_chunk_chars: int = _MAX_CHUNK_CHARS) -> None:
        self._max_chunk_chars = max_chunk_chars

    def parse(self, content: str, doc_name: str) -> List[DocumentNode]:
        """Parseia conteúdo e retorna nós com vínculos corretos."""
        if not content or not content.strip():
            return []

        if doc_name.lower().endswith(".pdf"):
            content = self._clean_pdf_text(content)

        sections = self._extract_sections(content)

        if not sections:
            return self._chunk_flat(content, doc_name)

        nodes: List[DocumentNode] = []
        parent_stack: List[tuple[int, str]] = []  # (level, node_id)

        for idx, (level, title, body) in enumerate(sections):
            node_id = self._make_id(doc_name, idx)

            parent_id = self._resolve_parent(parent_stack, level)

            node = DocumentNode(
                id=node_id,
                doc_name=doc_name,
                level=level,
                title=title,
                content=body.strip() if body else title,
                parent_id=parent_id,
            )
            nodes.append(node)

            parent_stack = [(lvl, nid) for lvl, nid in parent_stack if lvl < level]
            parent_stack.append((level, node_id))

        self._link_children(nodes)
        self._subdivide_large_leaves(nodes, doc_name)
        return nodes

    # ── extração de seções ──────────────────────────────────────────

    def _extract_sections(self, content: str) -> List[tuple[int, str, str]]:
        """Extrai (level, title, body) de headings Markdown e numerados."""
        lines = content.splitlines()
        sections: List[tuple[int, str, str]] = []

        current_heading: Optional[Tuple[int, str]] = None
        current_body: List[str] = []
        preamble: List[str] = []
        saw_heading = False

        for line in lines:
            heading = self._detect_heading(line)
            if heading:
                saw_heading = True
                if current_heading is None:
                    if preamble:
                        sections.append(
                            (0, "Introdução", "\n".join(preamble).strip())
                        )
                else:
                    sections.append(
                        (
                            current_heading[0],
                            current_heading[1],
                            "\n".join(current_body).strip(),
                        )
                    )

                current_heading = heading
                current_body = []
                continue

            if current_heading is None and not saw_heading:
                preamble.append(line)
            elif current_heading is not None:
                current_body.append(line)

        if current_heading is None:
            return []

        sections.append(
            (
                current_heading[0],
                current_heading[1],
                "\n".join(current_body).strip(),
            )
        )
        return sections

    def _detect_heading(self, line: str) -> Optional[Tuple[int, str]]:
        """Detecta headings em Markdown, numeração e blocos de unidade."""
        stripped = line.strip()
        if not stripped:
            return None

        markdown_match = _MARKDOWN_HEADING_RE.match(stripped)
        if markdown_match:
            level = len(markdown_match.group(1)) - 1
            return level, markdown_match.group(2).strip()

        unit_match = _UNIT_HEADING_RE.match(stripped)
        if unit_match:
            return 0, stripped

        numbered_match = _NUMBERED_HEADING_RE.match(stripped)
        if numbered_match:
            number = numbered_match.group(1)
            title_text = numbered_match.group(2).strip()
            if not self._looks_like_heading_title(number, title_text):
                return None
            level = max(0, number.count("."))
            return level, stripped

        return None

    @staticmethod
    def _looks_like_heading_title(number: str, title_text: str) -> bool:
        """Evita falsos positivos de linhas numeradas que são texto corrido."""
        if not title_text:
            return False

        char_limit = 90 if number.count(".") == 0 else 110
        word_limit = 10 if number.count(".") == 0 else 14

        if len(title_text) > char_limit:
            return False
        if len(title_text.split()) > word_limit:
            return False
        if title_text.endswith((".", ";", ":", ",")):
            return False

        return True

    def _chunk_flat(self, content: str, doc_name: str) -> List[DocumentNode]:
        """Divide conteúdo sem headings em chunks por tamanho."""
        chunks = self._split_by_size(content, self._max_chunk_chars)
        nodes: List[DocumentNode] = []
        for idx, chunk in enumerate(chunks):
            nodes.append(
                DocumentNode(
                    id=self._make_id(doc_name, idx),
                    doc_name=doc_name,
                    level=0,
                    title=f"Chunk {idx + 1}",
                    content=chunk,
                )
            )
        return nodes

    # ── sub-chunking ────────────────────────────────────────────────

    def _subdivide_large_leaves(
        self, nodes: List[DocumentNode], doc_name: str
    ) -> None:
        """Subdivide folhas com conteúdo maior que ``_max_chunk_chars``.

        Transforma a folha em nó interno e cria filhos com chunks
        menores, garantindo embeddings representativos.
        """
        leaves_to_split = [
            n for n in nodes if n.is_leaf and len(n.content) > self._max_chunk_chars
        ]
        if not leaves_to_split:
            return

        next_index = len(nodes)
        for leaf in leaves_to_split:
            chunks = self._split_by_size(leaf.content, self._max_chunk_chars)
            if len(chunks) <= 1:
                continue

            for i, chunk in enumerate(chunks):
                child_id = self._make_id(doc_name, next_index)
                next_index += 1
                child = DocumentNode(
                    id=child_id,
                    doc_name=doc_name,
                    level=leaf.level + 1,
                    title=f"{leaf.title} — parte {i + 1}",
                    content=chunk,
                    parent_id=leaf.id,
                )
                nodes.append(child)
                leaf.children_ids.append(child_id)

    # ── limpeza de texto ────────────────────────────────────────────

    @staticmethod
    def _clean_pdf_text(content: str) -> str:
        """Remove artefatos comuns de extração de PDF."""
        lines = content.splitlines()
        cleaned: List[str] = []
        for line in lines:
            stripped = line.strip()
            if _PAGE_NUMBER_RE.match(stripped):
                continue
            cleaned.append(line)

        text = "\n".join(cleaned)
        text = re.sub(r"\n{3,}", "\n\n", text)
        return text.strip()

    # ── helpers ─────────────────────────────────────────────────────

    @staticmethod
    def _resolve_parent(
        stack: List[tuple[int, str]], current_level: int
    ) -> Optional[str]:
        """Encontra o pai correto na pilha (último nó com nível menor)."""
        for lvl, nid in reversed(stack):
            if lvl < current_level:
                return nid
        return None

    @staticmethod
    def _link_children(nodes: List[DocumentNode]) -> None:
        """Preenche ``children_ids`` baseado em ``parent_id``."""
        id_to_node = {n.id: n for n in nodes}
        for node in nodes:
            if node.parent_id and node.parent_id in id_to_node:
                parent = id_to_node[node.parent_id]
                if node.id not in parent.children_ids:
                    parent.children_ids.append(node.id)

    @staticmethod
    def _make_id(doc_name: str, index: int) -> str:
        """Gera ID determinístico baseado em doc_name e posição."""
        return f"{doc_name}::node::{index}"

    @staticmethod
    def _split_by_size(text: str, max_chars: int) -> List[str]:
        """Divide texto em chunks respeitando quebras de parágrafo."""
        paragraphs = text.split("\n\n")
        chunks: List[str] = []
        current: List[str] = []
        current_len = 0

        for para in paragraphs:
            para = para.strip()
            if not para:
                continue
            if current_len + len(para) > max_chars and current:
                chunks.append("\n\n".join(current))
                current = []
                current_len = 0
            current.append(para)
            current_len += len(para)

        if current:
            chunks.append("\n\n".join(current))
        return chunks if chunks else [text]
