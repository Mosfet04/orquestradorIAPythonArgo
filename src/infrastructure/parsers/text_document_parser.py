"""Parser de documentos texto/markdown em árvore hierárquica."""

from __future__ import annotations

import re
import uuid
from typing import Dict, List, Optional

from src.domain.entities.document_node import DocumentNode
from src.domain.ports.document_parser_port import IDocumentParser

_HEADING_RE = re.compile(r"^(#{1,6})\s+(.+)$", re.MULTILINE)
_MAX_CHUNK_CHARS = 2000


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

        sections = self._extract_sections(content)

        if not sections:
            return self._chunk_flat(content, doc_name)

        nodes: List[DocumentNode] = []
        id_by_key: Dict[str, str] = {}
        parent_stack: List[tuple[int, str]] = []  # (level, node_id)

        for idx, (level, title, body) in enumerate(sections):
            node_id = self._make_id(doc_name, idx)
            id_by_key[node_id] = node_id

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

            parent_stack = [
                (lvl, nid) for lvl, nid in parent_stack if lvl < level
            ]
            parent_stack.append((level, node_id))

        self._link_children(nodes)
        return nodes

    # ── extração de seções ──────────────────────────────────────────

    def _extract_sections(
        self, content: str
    ) -> List[tuple[int, str, str]]:
        """Extrai (level, title, body) de cada heading encontrado."""
        matches = list(_HEADING_RE.finditer(content))
        if not matches:
            return []

        sections: List[tuple[int, str, str]] = []

        # Conteúdo antes do primeiro heading (se houver)
        preamble = content[: matches[0].start()].strip()
        if preamble:
            sections.append((0, "Introdução", preamble))

        for i, match in enumerate(matches):
            level = len(match.group(1)) - 1  # # → 0, ## → 1, ### → 2
            title = match.group(2).strip()
            start = match.end()
            end = matches[i + 1].start() if i + 1 < len(matches) else len(content)
            body = content[start:end].strip()
            sections.append((level, title, body))

        return sections

    def _chunk_flat(
        self, content: str, doc_name: str
    ) -> List[DocumentNode]:
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
