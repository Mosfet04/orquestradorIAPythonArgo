"""Serviço de indexação hierárquica de documentos."""

from __future__ import annotations

import asyncio
from concurrent.futures import ThreadPoolExecutor
from typing import Any, List

from src.domain.entities.document_node import DocumentNode
from src.domain.entities.rag_config import RagConfig
from src.domain.ports.document_parser_port import IDocumentParser
from src.domain.ports.document_tree_repository_port import IDocumentTreeRepository
from src.domain.ports.embedder_factory_port import IEmbedderFactory
from src.domain.ports.logger_port import ILogger
from src.domain.ports.summary_generator_port import ISummaryGenerator

_SUMMARY_BATCH_SIZE = 5
_EMBEDDING_MAX_CHARS = 1500
_EMBEDDING_WORKERS = 4


class DocumentIndexingService:
    """Indexa documentos em árvore hierárquica para busca top-down.

    Fluxo:
    1. Verifica idempotência (documento já indexado?).
    2. Parseia conteúdo em nós hierárquicos.
    3. Gera sumários para nós internos (paralelo em batches).
    4. Computa embeddings de todos os nós.
    5. Persiste no repositório.
    """

    def __init__(
        self,
        *,
        parser: IDocumentParser,
        tree_repository: IDocumentTreeRepository,
        summary_generator: ISummaryGenerator,
        embedder_factory: IEmbedderFactory,
        logger: ILogger,
    ) -> None:
        self._parser = parser
        self._tree_repo = tree_repository
        self._summary_gen = summary_generator
        self._embedder_factory = embedder_factory
        self._logger = logger

    async def index_document(
        self,
        doc_name: str,
        content: str,
        rag_config: RagConfig,
        *,
        force_reindex: bool = False,
    ) -> List[DocumentNode]:
        """Indexa documento caso ainda não exista.

        Parameters
        ----------
        force_reindex:
            Se True, remove nós existentes e re-indexa.

        Returns
        -------
        List[DocumentNode]
            Lista de nós criados (vazia se já existia e force_reindex=False).
        """
        if await self._tree_repo.exists(doc_name):
            if not force_reindex:
                self._logger.info("Documento já indexado — skip", doc_name=doc_name)
                return []

        self._logger.info("Iniciando indexação hierárquica", doc_name=doc_name)

        nodes = self._parser.parse(content, doc_name)
        if not nodes:
            self._logger.warning("Parser retornou zero nós", doc_name=doc_name)
            return []

        factory_type = rag_config.resolved_provider
        model_id = rag_config.resolved_model
        embedder = self._embedder_factory.create_model(
            factory_type, model_id,
        )

        await self._generate_summaries(nodes)
        await self._compute_embeddings(nodes, embedder)

        if force_reindex:
            await self._tree_repo.replace_nodes(doc_name, nodes)
        else:
            await self._tree_repo.save_nodes(nodes)

        self._logger.info(
            "Indexação concluída",
            doc_name=doc_name,
            total_nodes=len(nodes),
        )
        return nodes

    # ── private ─────────────────────────────────────────────────────

    async def _generate_summaries(self, nodes: List[DocumentNode]) -> None:
        """Gera sumários para nós internos (não-folha) em batches."""
        internal_nodes = [n for n in nodes if not n.is_leaf]
        if not internal_nodes:
            return

        for i in range(0, len(internal_nodes), _SUMMARY_BATCH_SIZE):
            batch = internal_nodes[i : i + _SUMMARY_BATCH_SIZE]
            tasks = [self._safe_summarize(node) for node in batch]
            await asyncio.gather(*tasks)

    async def _safe_summarize(self, node: DocumentNode) -> None:
        """Gera sumário com fallback para os primeiros 200 chars."""
        try:
            node.summary = await self._summary_gen.generate_summary(node.content)
        except Exception as exc:
            self._logger.warning(
                "Fallback de sumário",
                node_id=node.id,
                error=str(exc),
            )
            node.summary = node.content[:200]

    async def _compute_embeddings(self, nodes: List[DocumentNode], embedder: Any) -> None:
        """Computa embeddings em paralelo usando ThreadPoolExecutor."""
        loop = asyncio.get_running_loop()

        def _embed_single(node: DocumentNode) -> None:
            text = node.searchable_text
            if not text:
                return
            if len(text) > _EMBEDDING_MAX_CHARS:
                self._logger.info(
                    "Texto truncado para embedding",
                    node_id=node.id,
                    original_len=len(text),
                    truncated_to=_EMBEDDING_MAX_CHARS,
                )
            text = text[:_EMBEDDING_MAX_CHARS]
            try:
                embedding = embedder.get_embedding(text)
                normalized = self._normalize_embedding(embedding)
                if normalized:
                    node.embedding = normalized
                else:
                    self._logger.warning("Embedding vazio retornado", node_id=node.id)
            except Exception as exc:
                self._logger.warning(
                    "Erro ao computar embedding",
                    node_id=node.id,
                    error=str(exc),
                )

        with ThreadPoolExecutor(max_workers=_EMBEDDING_WORKERS) as pool:
            futures = [
                loop.run_in_executor(pool, _embed_single, node)
                for node in nodes
                if node.searchable_text
            ]
            await asyncio.gather(*futures)

    @staticmethod
    def _normalize_embedding(embedding: Any) -> List[float]:
        """Converte embeddings retornados em qualquer formato comum para ``list``."""
        if embedding is None:
            return []
        if hasattr(embedding, "tolist"):
            embedding = embedding.tolist()
        if isinstance(embedding, tuple):
            embedding = list(embedding)
        if isinstance(embedding, list):
            return embedding
        return []
