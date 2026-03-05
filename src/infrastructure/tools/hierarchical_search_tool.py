"""Tool de busca hierárquica para injeção em agentes agno."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from src.domain.ports.knowledge_search_port import IKnowledgeSearchStrategy


def create_hierarchical_search_tool(
    strategy: IKnowledgeSearchStrategy,
    *,
    top_k: int = 5,
) -> Callable[..., str]:
    """Cria uma função callable que agentes agno podem usar como tool.

    A função retornada encapsula a estratégia hierárquica e retorna
    os chunks mais relevantes como texto formatado.

    Parameters
    ----------
    strategy:
        Estratégia de busca hierárquica já configurada.
    top_k:
        Número máximo de resultados.

    Returns
    -------
    Callable
        Função ``search_knowledge(query: str) -> str``.
    """

    def search_knowledge(query: str) -> str:
        """Busca informações relevantes no knowledge base hierárquico.

        Args:
            query: A pergunta ou termo de busca.

        Returns:
            Trechos relevantes encontrados no knowledge base.
        """
        try:
            loop = asyncio.get_event_loop()
            if loop.is_running():
                import concurrent.futures
                with concurrent.futures.ThreadPoolExecutor() as pool:
                    results = pool.submit(
                        asyncio.run, strategy.search(query, top_k=top_k)
                    ).result()
            else:
                results = asyncio.run(strategy.search(query, top_k=top_k))
        except Exception:
            results = []

        if not results:
            return "Nenhuma informação relevante encontrada no knowledge base."

        parts = []
        for r in results:
            header = r.metadata.get("title", r.node_id)
            parts.append(f"[{header}] (score: {r.score:.2f})\n{r.content}")

        return "\n\n---\n\n".join(parts)

    return search_knowledge
