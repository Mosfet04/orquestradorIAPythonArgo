"""Entidade que representa o resultado de uma busca no knowledge base."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict


@dataclass
class SearchResult:
    """Resultado unificado retornado por qualquer estratégia de busca RAG.

    Independe de o retrieval ter sido semântico ou hierárquico.
    """

    content: str
    score: float
    node_id: str = ""
    metadata: Dict[str, str] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.content:
            raise ValueError("content do resultado não pode estar vazio")
        if not 0.0 <= self.score <= 1.0:
            raise ValueError("score deve estar entre 0.0 e 1.0")
