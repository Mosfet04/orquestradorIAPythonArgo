"""Port para estratégias de busca no knowledge base."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from src.domain.entities.search_result import SearchResult


class IKnowledgeSearchStrategy(ABC):
    """Interface Strategy para diferentes mecanismos de retrieval RAG.

    Permite trocar entre busca semântica, hierárquica ou qualquer
    outra estratégia futura sem alterar o domínio.
    """

    @abstractmethod
    async def search(self, query: str, *, top_k: int = 5) -> List[SearchResult]:
        """Executa busca e retorna os resultados mais relevantes."""
        ...
