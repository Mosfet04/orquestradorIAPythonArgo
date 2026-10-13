"""Serviço de indexação hierárquica de documentos."""

from __future__ import annotations

import asyncio
from typing import Dict, List

from src.domain.entities.document_node import DocumentNode
from src.domain.entities.rag_config import RagConfig
from src.domain.ports.document_parser_port import IDocumentParser
from src.domain.ports.document_tree_repository_port import IDocumentTreeRepository
from src.domain.ports.embedder_factory_port import IEmbedderFactory, TextEmbedder
from src.domain.ports.logger_port import ILogger
from src.domain.ports.summary_generator_port import (
    ISummaryGenerator,
    SummaryTimeoutError,
)

_SUMMARY_BATCH_SIZE = 5


class EmbeddingRejectedError(RuntimeError):
    """Nó com embedding vazio, de tamanho diferente ou que falhou: a árvore não é gravada (F2-11)."""


class DocumentIndexingService:
    """Indexa documentos em árvore hierárquica para busca top-down.

    Fluxo:
    1. Verifica idempotência (documento já indexado e sem ``embedding: []`` de antes do F2-11?).
    2. Parseia conteúdo em nós hierárquicos.
    3. Gera sumários para nós internos (paralelo em batches).
    4. Computa embeddings de todos os nós; qualquer um inválido (vazio, de tamanho diferente
       dos demais ou com erro) recusa o documento inteiro (``EmbeddingRejectedError``).
    5. Troca a árvore antiga (se havia) pela nova e persiste no repositório.
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
        # Single-flight por documento (BUG-F1-07-QA-1): o startup cria os agentes com
        # gather e dois agentes no mesmo doc_name passariam juntos pelo exists().
        # Um Lock por doc_name configurado: cresce só com a configuração.
        self._locks: Dict[str, asyncio.Lock] = {}

    async def index_document(
        self,
        doc_name: str,
        content: str,
        rag_config: RagConfig,
    ) -> List[DocumentNode]:
        """Indexa documento caso ainda não exista.

        Chamadas concorrentes para o mesmo ``doc_name`` são serializadas neste processo:
        a segunda espera a primeira e encontra o documento já indexado. Embedding inválido
        levanta ``EmbeddingRejectedError`` sem gravar nada (a árvore antiga, se havia, fica).

        Returns
        -------
        List[DocumentNode]
            Lista de nós criados (vazia se já existia).
        """
        lock = self._locks.setdefault(doc_name, asyncio.Lock())
        if lock.locked():
            self._logger.debug(
                "Indexação do documento em andamento; aguardando", doc_name=doc_name
            )
        async with lock:
            return await self._index_if_missing(doc_name, content, rag_config)

    async def _index_if_missing(
        self,
        doc_name: str,
        content: str,
        rag_config: RagConfig,
    ) -> List[DocumentNode]:
        stale = False
        if await self._tree_repo.exists(doc_name):
            if not await self._tree_repo.has_empty_embedding(doc_name):
                self._logger.info(
                    "Documento já indexado — skip", doc_name=doc_name
                )
                return []
            stale = True
            self._logger.warning(
                "Árvore do documento com embedding vazio (gravada antes do F2-11); reindexando",
                doc_name=doc_name,
            )

        self._logger.info("Iniciando indexação hierárquica", doc_name=doc_name)

        nodes = self._parser.parse(content, doc_name)
        if not nodes:
            self._logger.warning("Parser retornou zero nós", doc_name=doc_name)
            return []

        # A criação pode ler segredo (file:) e resolver DNS do destino: fora do event loop.
        embedder = await asyncio.to_thread(
            self._embedder_factory.create_embedder, rag_config.embedder_model_config()
        )

        await self._generate_summaries(doc_name, nodes)
        # get_embedding é síncrono (rede no provider real): fora do event loop
        await asyncio.to_thread(self._compute_embeddings, doc_name, nodes, embedder)

        if stale:
            deleted = await self._tree_repo.delete_document(doc_name)
            self._logger.info("Árvore antiga do documento apagada", doc_name=doc_name, deleted=deleted)
        await self._tree_repo.save_nodes(nodes)
        self._logger.info(
            "Indexação concluída",
            doc_name=doc_name,
            total_nodes=len(nodes),
        )
        return nodes

    # ── private ─────────────────────────────────────────────────────

    async def _generate_summaries(
        self, doc_name: str, nodes: List[DocumentNode]
    ) -> None:
        """Gera sumários para nós internos (não-folha) em batches.

        Depois do primeiro ``SummaryTimeoutError`` do documento, os nós restantes vão
        direto ao fallback (modelo travado não custa um timeout por nó no startup).
        """
        internal_nodes = [n for n in nodes if not n.is_leaf]
        if not internal_nodes:
            return

        timed_out = asyncio.Event()  # só uma flag, por documento
        for i in range(0, len(internal_nodes), _SUMMARY_BATCH_SIZE):
            batch = internal_nodes[i : i + _SUMMARY_BATCH_SIZE]
            tasks = [
                self._safe_summarize(doc_name, node, timed_out) for node in batch
            ]
            await asyncio.gather(*tasks)

    async def _safe_summarize(
        self, doc_name: str, node: DocumentNode, timed_out: asyncio.Event
    ) -> None:
        """Gera sumário com fallback para os primeiros 200 chars."""
        if timed_out.is_set():
            node.summary = node.content[:200]
            return
        try:
            node.summary = await self._summary_gen.generate_summary(
                node.content
            )
        except SummaryTimeoutError as exc:
            if not timed_out.is_set():
                timed_out.set()
                self._logger.warning(
                    "Modelo de sumário sem resposta; demais nós do documento "
                    "usam truncamento",
                    doc_name=doc_name,
                    error_type=type(exc).__name__,
                )
            node.summary = node.content[:200]
        except Exception as exc:
            self._logger.warning(
                "Fallback de sumário",
                node_id=node.id,
                error_type=type(exc).__name__,
            )
            node.summary = node.content[:200]

    def _compute_embeddings(
        self, doc_name: str, nodes: List[DocumentNode], embedder: TextEmbedder
    ) -> None:
        """Computa embedding para cada nó com texto; algum inválido levanta ``EmbeddingRejectedError``.

        Inválido: não é lista, é vazio, tem tamanho diferente do primeiro válido ou o embedder
        falhou. Nó sem texto fica com ``None``, como antes.
        """
        rejected = total = 0
        size: int | None = None
        for node in nodes:
            text = node.searchable_text
            if not text:
                continue
            total += 1
            embedding = self._embedding_of(node, text, embedder)
            if not embedding or (size is not None and len(embedding) != size):
                rejected += 1
                continue
            size = len(embedding)
            node.embedding = embedding
        if rejected:
            self._logger.error(
                "Embedding vazio, de tamanho diferente ou com erro; documento não indexado",
                doc_name=doc_name,
                rejected=rejected,
                total=total,
                error_type=EmbeddingRejectedError.__name__,
            )
            raise EmbeddingRejectedError(
                f"{rejected} de {total} nós sem embedding válido; documento não indexado"
            )

    def _embedding_of(
        self, node: DocumentNode, text: str, embedder: TextEmbedder
    ) -> List[float] | None:
        try:
            embedding = embedder.get_embedding(text)
        except Exception as exc:
            self._logger.warning(
                "Erro ao computar embedding",
                node_id=node.id,
                error_type=type(exc).__name__,
            )
            return None
        return embedding if isinstance(embedding, list) else None
