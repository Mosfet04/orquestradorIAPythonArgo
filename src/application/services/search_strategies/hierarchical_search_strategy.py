"""Estratégia de busca hierárquica — travessia top-down por sumário."""

from __future__ import annotations

import asyncio
import math
from typing import Any, List, Optional

from src.domain.entities.document_node import DocumentNode
from src.domain.entities.search_result import SearchResult
from src.domain.ports.document_tree_repository_port import IDocumentTreeRepository
from src.domain.ports.knowledge_search_port import IKnowledgeSearchStrategy
from src.domain.ports.logger_port import ILogger

# Similaridade cosseno mínima de um resultado (F1-07, B9). Piso conservador: descarta o
# claramente não relacionado (e score 0.0: nó sem embedding ou de dimensão diferente) sem
# cortar trecho relevante de embedders cuja escala é baixa (ex.: OpenAI text-embedding-3,
# relevante costuma ficar em 0.3-0.6). O 0.85 anterior nunca filtrava nada; como filtro,
# esvaziaria a busca com os embedders usados aqui. Calibrar por embedder com eval.
_MIN_CONFIDENCE = 0.3
_BEAM_WIDTH = 2  # nós explorados por nível


class HierarchicalSearchStrategy(IKnowledgeSearchStrategy):
    """Busca hierárquica top-down por sumário + árvore de decisão.

    Fluxo:
    1. Computa embedding da query.
    2. Busca nós raiz do documento.
    3. Calcula similaridade cosseno entre query e sumário de cada nó raiz.
    4. Seleciona os ``beam_width`` melhores nós.
    5. Desce recursivamente para os filhos do(s) melhor(es) nó(s).
    6. Repete até chegar a nós folha.
    7. Descarta resultados com score abaixo de ``confidence_threshold``.
    8. Retorna os chunks finais como ``List[SearchResult]``.
    """

    def __init__(
        self,
        *,
        tree_repository: IDocumentTreeRepository,
        embedder: Any,
        doc_name: str,
        logger: ILogger,
        beam_width: int = _BEAM_WIDTH,
        confidence_threshold: float = _MIN_CONFIDENCE,
    ) -> None:
        self._tree_repo = tree_repository
        self._embedder = embedder
        self._doc_name = doc_name
        self._logger = logger
        self._beam_width = beam_width
        self._confidence_threshold = confidence_threshold

    # ── public ──────────────────────────────────────────────────────

    async def search(self, query: str, *, top_k: int = 5) -> List[SearchResult]:
        """Executa busca top-down na árvore hierárquica."""
        query_embedding = await self._compute_embedding(query)
        if query_embedding is None:
            self._logger.warning("Falha ao computar embedding da query")
            return []

        root_nodes = await self._tree_repo.get_root_nodes(self._doc_name)
        if not root_nodes:
            self._logger.warning(
                "Nenhum nó raiz encontrado", doc_name=self._doc_name
            )
            return []

        leaf_results = await self._traverse(root_nodes, query_embedding)
        confident = [r for r in leaf_results if r.score >= self._confidence_threshold]
        discarded = len(leaf_results) - len(confident)
        if discarded:
            # Sem conteúdo no log: só o necessário para calibrar o limiar.
            self._logger.debug(
                "Resultados abaixo do limiar descartados",
                doc_name=self._doc_name,
                discarded=discarded,
                best_score=round(max(r.score for r in leaf_results), 4),
                threshold=self._confidence_threshold,
            )
        confident.sort(key=lambda r: r.score, reverse=True)
        return confident[:top_k]

    # ── travessia ───────────────────────────────────────────────────

    async def _traverse(
        self,
        nodes: List[DocumentNode],
        query_embedding: List[float],
    ) -> List[SearchResult]:
        """Desce recursivamente pela árvore, selecionando os melhores nós."""
        scored = self._rank_nodes(nodes, query_embedding)
        best = scored[: self._beam_width]

        results: List[SearchResult] = []
        for node, score in best:
            if node.is_leaf:
                results.append(
                    SearchResult(
                        content=node.content,
                        score=score,
                        node_id=node.id,
                        metadata={
                            "strategy": "hierarchical",
                            "level": str(node.level),
                            "title": node.title,
                        },
                    )
                )
            else:
                # O limiar vale para o resultado final (``search``), não para a descida:
                # sumário com score baixo pode ter filho relevante.
                children = await self._tree_repo.get_children(node.id)
                if children:
                    results.extend(await self._traverse(children, query_embedding))
                else:
                    results.append(self._node_to_result(node, score))
        return results

    # ── scoring ─────────────────────────────────────────────────────

    def _rank_nodes(
        self,
        nodes: List[DocumentNode],
        query_embedding: List[float],
    ) -> List[tuple[DocumentNode, float]]:
        """Ordena nós por similaridade cosseno com a query."""
        scored: List[tuple[DocumentNode, float]] = []
        for node in nodes:
            if node.embedding is None:
                scored.append((node, 0.0))
                continue
            sim = self._cosine_similarity(query_embedding, node.embedding)
            scored.append((node, sim))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    @staticmethod
    def _cosine_similarity(vec_a: List[float], vec_b: List[float]) -> float:
        """Similaridade cosseno entre dois vetores."""
        if len(vec_a) != len(vec_b) or not vec_a:
            return 0.0
        dot = sum(a * b for a, b in zip(vec_a, vec_b))
        norm_a = math.sqrt(sum(a * a for a in vec_a))
        norm_b = math.sqrt(sum(b * b for b in vec_b))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return max(0.0, min(1.0, dot / (norm_a * norm_b)))

    # ── helpers ─────────────────────────────────────────────────────

    async def _compute_embedding(self, text: str) -> Optional[List[float]]:
        """Computa embedding via embedder injetado (síncrono, com rede: fora do loop)."""
        try:
            result = await asyncio.to_thread(self._embedder.get_embedding, text)
            return result if isinstance(result, list) else None
        except Exception as exc:
            self._logger.warning("Erro ao computar embedding", error=str(exc))
            return None

    @staticmethod
    def _node_to_result(node: DocumentNode, score: float) -> SearchResult:
        return SearchResult(
            content=node.content,
            score=score,
            node_id=node.id,
            metadata={
                "strategy": "hierarchical",
                "level": str(node.level),
                "title": node.title,
            },
        )
