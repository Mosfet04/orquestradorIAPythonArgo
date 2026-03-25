"""Estratégia de busca hierárquica — travessia top-down por sumário."""

from __future__ import annotations

import math
from typing import Any, List, Optional

from src.domain.entities.document_node import DocumentNode
from src.domain.entities.search_result import SearchResult
from src.domain.ports.document_tree_repository_port import IDocumentTreeRepository
from src.domain.ports.knowledge_search_port import IKnowledgeSearchStrategy
from src.domain.ports.logger_port import ILogger

_HIGH_CONFIDENCE_THRESHOLD = 0.85
_MIN_SCORE_THRESHOLD = 0.3
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
    7. Retorna chunks finais como ``List[SearchResult]``.
    """

    def __init__(
        self,
        *,
        tree_repository: IDocumentTreeRepository,
        embedder: Any,
        doc_name: str,
        logger: ILogger,
        beam_width: int = _BEAM_WIDTH,
        confidence_threshold: float = _HIGH_CONFIDENCE_THRESHOLD,
    ) -> None:
        self._tree_repo = tree_repository
        self._embedder = embedder
        self._doc_name = doc_name
        self._logger = logger
        self._beam_width = beam_width
        self._confidence_threshold = confidence_threshold
        self._min_score_threshold = _MIN_SCORE_THRESHOLD

    # ── public ──────────────────────────────────────────────────────

    async def search(self, query: str, *, top_k: int = 5) -> List[SearchResult]:
        """Executa busca top-down na árvore hierárquica."""
        query_embedding = self._compute_embedding(query)
        if query_embedding is None:
            self._logger.warning("Falha ao computar embedding da query")
            return []

        root_nodes = await self._tree_repo.get_root_nodes(self._doc_name)
        if not root_nodes:
            self._logger.warning("Nenhum nó raiz encontrado", doc_name=self._doc_name)
            return []

        leaf_results = await self._traverse(root_nodes, query_embedding)
        leaf_results.sort(key=lambda r: r.score, reverse=True)
        return leaf_results[:top_k]

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
        expand_nodes: List[tuple[DocumentNode, float]] = []

        for node, score in best:
            if score < self._min_score_threshold:
                continue
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
                expand_nodes.append((node, score))

        if expand_nodes:
            parent_ids = [n.id for n, _ in expand_nodes]
            all_children = await self._tree_repo.get_children_batch(parent_ids)
            children_by_parent: dict[str, List[DocumentNode]] = {}
            for child in all_children:
                children_by_parent.setdefault(child.parent_id, []).append(child)

            for node, score in expand_nodes:
                children = children_by_parent.get(node.id, [])
                if children:
                    child_results = await self._traverse(children, query_embedding)
                    results.extend(child_results)
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
        query_norm = math.sqrt(math.fsum(a * a for a in query_embedding))
        if query_norm == 0.0:
            return [(node, 0.0) for node in nodes]

        scored: List[tuple[DocumentNode, float]] = []
        for node in nodes:
            if node.embedding is None:
                scored.append((node, 0.0))
                continue
            sim = self._cosine_similarity(query_embedding, node.embedding, query_norm)
            scored.append((node, sim))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored

    @staticmethod
    def _cosine_similarity(
        vec_a: List[float],
        vec_b: List[float],
        norm_a: float = 0.0,
    ) -> float:
        """Similaridade cosseno entre dois vetores.

        Se ``norm_a`` for fornecido, evita recalculá-lo (útil quando
        ``vec_a`` é o embedding da query, reutilizado muitas vezes).
        """
        if len(vec_a) != len(vec_b) or not vec_a:
            return 0.0
        dot = math.fsum(a * b for a, b in zip(vec_a, vec_b))
        if norm_a == 0.0:
            norm_a = math.sqrt(math.fsum(a * a for a in vec_a))
        norm_b = math.sqrt(math.fsum(b * b for b in vec_b))
        if norm_a == 0.0 or norm_b == 0.0:
            return 0.0
        return max(0.0, min(1.0, dot / (norm_a * norm_b)))

    # ── helpers ─────────────────────────────────────────────────────

    def _compute_embedding(self, text: str) -> Optional[List[float]]:
        """Computa embedding via embedder injetado."""
        try:
            result = self._embedder.get_embedding(text)
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
