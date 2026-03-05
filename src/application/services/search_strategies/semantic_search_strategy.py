"""Estratégia de busca semântica — wrapper sobre o Knowledge do agno."""

from __future__ import annotations

from typing import Any, List

from src.domain.entities.search_result import SearchResult
from src.domain.ports.knowledge_search_port import IKnowledgeSearchStrategy


class SemanticSearchStrategy(IKnowledgeSearchStrategy):
    """Busca semântica via vector store (comportamento atual do agno).

    Encapsula a instância de ``Knowledge`` do agno e delega a busca
    vetorial ao framework, convertendo os resultados para
    ``SearchResult`` do domínio.
    """

    def __init__(self, knowledge: Any) -> None:
        self._knowledge = knowledge

    async def search(self, query: str, *, top_k: int = 5) -> List[SearchResult]:
        """Busca semântica no vector store."""
        raw_results = self._knowledge.search(query=query, num_documents=top_k)

        results: List[SearchResult] = []
        if not raw_results:
            return results

        for idx, doc in enumerate(raw_results):
            content = doc.content if hasattr(doc, "content") else str(doc)
            name = doc.name if hasattr(doc, "name") else ""
            score = 1.0 - (idx * 0.05)  # score estimado por posição
            results.append(
                SearchResult(
                    content=content,
                    score=max(score, 0.0),
                    node_id=name,
                    metadata={"strategy": "semantic"},
                )
            )
        return results
