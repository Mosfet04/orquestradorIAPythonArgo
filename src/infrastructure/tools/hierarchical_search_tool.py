"""Tool de busca hierárquica para injeção em agentes agno."""

from __future__ import annotations

from agno.tools import Toolkit

from src.domain.ports.knowledge_search_port import IKnowledgeSearchStrategy


def create_hierarchical_search_tool(
    strategy: IKnowledgeSearchStrategy,
    *,
    top_k: int = 5,
) -> Toolkit:
    """Cria um ``Toolkit`` agno com função async de busca hierárquica.

    Usa o mesmo padrão do ``HttpToolFactory``: registra uma função
    ``async def`` no ``Toolkit`` para que o agente a execute dentro
    do event-loop corrente — sem necessidade de bridging sync→async.

    Parameters
    ----------
    strategy:
        Estratégia de busca hierárquica já configurada.
    top_k:
        Número máximo de resultados.

    Returns
    -------
    Toolkit
        Toolkit agno com a tool ``search_knowledge``.
    """

    async def search_knowledge(query: str) -> str:
        """Busca informações relevantes no knowledge base hierárquico.

        Args:
            query: A pergunta ou termo de busca.

        Returns:
            Trechos relevantes encontrados no knowledge base.
        """
        try:
            results = await strategy.search(query, top_k=top_k)
        except Exception as exc:
            return f"Erro ao buscar no knowledge base: {exc}"

        if not results:
            return "Nenhuma informação relevante encontrada no knowledge base."

        parts = []
        for r in results:
            header = r.metadata.get("title", r.node_id)
            parts.append(f"[{header}] (score: {r.score:.2f})\n{r.content}")

        return "\n\n---\n\n".join(parts)

    toolkit = Toolkit(
        name="hierarchical_search",
        instructions="Busca informações no knowledge base hierárquico do agente.",
    )
    toolkit.register(function=search_knowledge, name="search_knowledge")
    return toolkit
