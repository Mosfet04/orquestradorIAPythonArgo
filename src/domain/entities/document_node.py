"""Entidade que representa um nó na árvore hierárquica de documentos."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class DocumentNode:
    """Nó de uma árvore hierárquica de documento para busca por sumário.

    Cada nó representa uma seção do documento (heading) e pode conter
    filhos (sub-seções).  Nós folha contêm o conteúdo textual final;
    nós internos armazenam um *summary* gerado por LLM e o embedding
    correspondente para travessia top-down.
    """

    id: str
    doc_name: str
    level: int
    title: str
    content: str
    parent_id: Optional[str] = None
    summary: Optional[str] = None
    embedding: Optional[List[float]] = None
    children_ids: List[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("ID do nó não pode estar vazio")
        if not self.doc_name:
            raise ValueError("doc_name do nó não pode estar vazio")
        if self.level < 0:
            raise ValueError("level deve ser >= 0")
        if not self.title:
            raise ValueError("title do nó não pode estar vazio")

    @property
    def is_leaf(self) -> bool:
        """Retorna ``True`` se o nó não possui filhos."""
        return len(self.children_ids) == 0

    @property
    def searchable_text(self) -> str:
        """Texto usado para gerar embedding: summary (se existir) ou content."""
        return self.summary or self.content
