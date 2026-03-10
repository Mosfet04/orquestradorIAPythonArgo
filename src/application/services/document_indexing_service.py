"""Serviço de indexação hierárquica de documentos."""

from __future__ import annotations

import asyncio
from typing import Any, List

from src.domain.entities.document_node import DocumentNode
from src.domain.entities.rag_config import RagConfig
from src.domain.ports.document_parser_port import IDocumentParser
from src.domain.ports.document_tree_repository_port import IDocumentTreeRepository
from src.domain.ports.embedder_factory_port import IEmbedderFactory
from src.domain.ports.logger_port import ILogger
from src.domain.ports.summary_generator_port import ISummaryGenerator

_SUMMARY_BATCH_SIZE = 5


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
    ) -> List[DocumentNode]:
        """Indexa documento caso ainda não exista.

        Returns
        -------
        List[DocumentNode]
            Lista de nós criados (vazia se já existia).
        """
        if await self._tree_repo.exists(doc_name):
            self._logger.info(
                "Documento já indexado — skip", doc_name=doc_name
            )
            return []

        self._logger.info("Iniciando indexação hierárquica", doc_name=doc_name)

        nodes = self._parser.parse(content, doc_name)
        if not nodes:
            self._logger.warning("Parser retornou zero nós", doc_name=doc_name)
            return []

        embedder = self._embedder_factory.create_model(
            rag_config.factory_ia_model or "ollama",
            rag_config.model or "nomic-embed-text:latest",
        )

        await self._generate_summaries(nodes)
        self._compute_embeddings(nodes, embedder)

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
            tasks = [
                self._safe_summarize(node) for node in batch
            ]
            await asyncio.gather(*tasks)

    async def _safe_summarize(self, node: DocumentNode) -> None:
        """Gera sumário com fallback para os primeiros 200 chars."""
        try:
            node.summary = await self._summary_gen.generate_summary(
                node.content
            )
        except Exception as exc:
            self._logger.warning(
                "Fallback de sumário",
                node_id=node.id,
                error=str(exc),
            )
            node.summary = node.content[:200]

    def _compute_embeddings(
        self, nodes: List[DocumentNode], embedder: Any
    ) -> None:
        """Computa embedding para cada nó usando o texto adequado."""
        for node in nodes:
            text = node.searchable_text
            if not text:
                continue
            try:
                embedding = embedder.get_embedding(text)
                if isinstance(embedding, list):
                    node.embedding = embedding
            except Exception as exc:
                self._logger.warning(
                    "Erro ao computar embedding",
                    node_id=node.id,
                    error=str(exc),
                )
