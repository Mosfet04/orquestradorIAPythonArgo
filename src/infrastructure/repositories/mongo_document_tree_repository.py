"""Repositório MongoDB para árvore hierárquica de documentos."""

from __future__ import annotations

from typing import Any, ClassVar

from bson import ObjectId
from pymongo.errors import BulkWriteError

from src.domain.entities.document_node import DocumentNode
from src.domain.ports.document_tree_repository_port import IDocumentTreeRepository
from src.domain.ports.logger_port import ILogger
from src.infrastructure.repositories.mongo_base import AsyncMongoRepository

# Código do Mongo para violação de índice único (id de nó repetido).
_DUPLICATE_KEY = 11000


class MongoDocumentTreeRepository(AsyncMongoRepository, IDocumentTreeRepository):
    """Implementação async do repositório de árvore de documentos.

    Armazena nós na collection ``document_tree`` com índices
    otimizados para travessia hierárquica.

    Ordem (contrato do F2-06): a de gravação. Cada ``save_nodes`` grava um ``_batch`` (ObjectId,
    crescente no processo) e a posição do nó na lista (``_order``); as leituras ordenam pelos dois,
    então um lote anterior vem antes de um posterior (só ``_order`` intercalaria os lotes). Documento
    antigo sem ``_batch`` vem antes (``null`` é o menor valor), na ordem de ``_order`` de sempre.
    Id de nó repetido: vence o primeiro gravado (índice único ``idx_node_id``); ver ``save_nodes``.
    """

    _NODE_ORDER: ClassVar[list[tuple[str, int]]] = [("_batch", 1), ("_order", 1)]

    def __init__(
        self,
        *,
        connection_string: str,
        database_name: str = "agno",
        collection_name: str = "document_tree",
        logger: ILogger,
    ) -> None:
        super().__init__(
            connection_string=connection_string,
            database_name=database_name,
            collection_name=collection_name,
            logger=logger,
        )

    async def ensure_indexes(self) -> None:
        """Cria índices compostos para queries performáticas."""
        await self._collection.create_index(
            [("doc_name", 1), ("level", 1)],
            name="idx_doc_level",
        )
        await self._collection.create_index(
            [("parent_id", 1)],
            name="idx_parent",
        )
        await self._collection.create_index(
            [("id", 1)],
            name="idx_node_id",
            unique=True,
        )

    async def save_nodes(self, nodes: list[DocumentNode]) -> None:
        """Persiste nós em lote (insert_many), na ordem da lista e depois dos lotes anteriores.

        Id repetido (no lote ou contra nó já gravado): o índice único recusa só o repetido, os demais
        são gravados (``ordered=False``) e o primeiro gravado vence; vira log de erro com os ids, sem
        levantar. Qualquer outra falha de escrita sobe.
        """
        if not nodes:
            return
        batch = ObjectId()
        docs = [self._to_document(node, order=i, batch=batch) for i, node in enumerate(nodes)]
        try:
            await self._collection.insert_many(docs, ordered=False)
            self._logger.info("Nós salvos", count=len(docs))
        except BulkWriteError as exc:
            errors = exc.details.get("writeErrors", [])
            only_duplicates = bool(errors) and all(error.get("code") == _DUPLICATE_KEY for error in errors)
            if not only_duplicates or exc.details.get("writeConcernErrors"):
                self._logger.error("Erro ao salvar nós", error_type=type(exc).__name__)
                raise
            self._logger.error(
                "Nós com id repetido ignorados; vale o primeiro gravado",
                node_ids=[nodes[error["index"]].id for error in errors],
                saved=len(docs) - len(errors),
            )
        except Exception as exc:
            self._logger.error("Erro ao salvar nós", error=str(exc))
            raise

    async def get_root_nodes(self, doc_name: str) -> list[DocumentNode]:
        """Retorna nós raiz (level 0) de um documento."""
        cursor = self._collection.find({"doc_name": doc_name, "level": 0}).sort(self._NODE_ORDER)
        return [self._to_entity(doc) async for doc in cursor]

    async def get_children(self, parent_id: str) -> list[DocumentNode]:
        """Retorna filhos diretos de um nó."""
        cursor = self._collection.find({"parent_id": parent_id}).sort(self._NODE_ORDER)
        return [self._to_entity(doc) async for doc in cursor]

    async def get_node(self, node_id: str) -> DocumentNode | None:
        """Busca um nó pelo ID (o primeiro gravado, se o índice único faltar)."""
        doc = await self._collection.find_one({"id": node_id}, sort=self._NODE_ORDER)
        return self._to_entity(doc) if doc else None

    async def exists(self, doc_name: str) -> bool:
        """Verifica se o documento já está indexado."""
        count = await self._collection.count_documents({"doc_name": doc_name}, limit=1)
        return count > 0

    # ── mappers ─────────────────────────────────────────────────────

    @staticmethod
    def _to_document(node: DocumentNode, *, order: int, batch: ObjectId) -> dict[str, Any]:
        return {
            "id": node.id,
            "doc_name": node.doc_name,
            "level": node.level,
            "title": node.title,
            "content": node.content,
            "parent_id": node.parent_id,
            "summary": node.summary,
            "embedding": node.embedding,
            "children_ids": node.children_ids,
            "_order": order,
            "_batch": batch,
        }

    @staticmethod
    def _to_entity(data: dict[str, Any]) -> DocumentNode:
        return DocumentNode(
            id=data.get("id", ""),
            doc_name=data.get("doc_name", ""),
            level=data.get("level", 0),
            title=data.get("title", ""),
            content=data.get("content", ""),
            parent_id=data.get("parent_id"),
            summary=data.get("summary"),
            embedding=data.get("embedding"),
            children_ids=data.get("children_ids", []),
        )
