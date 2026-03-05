"""Repositório MongoDB para árvore hierárquica de documentos."""

from __future__ import annotations

from typing import List, Optional

from src.domain.entities.document_node import DocumentNode
from src.domain.ports.document_tree_repository_port import IDocumentTreeRepository
from src.domain.ports.logger_port import ILogger
from src.infrastructure.repositories.mongo_base import AsyncMongoRepository


class MongoDocumentTreeRepository(AsyncMongoRepository, IDocumentTreeRepository):
    """Implementação async do repositório de árvore de documentos.

    Armazena nós na collection ``document_tree`` com índices
    otimizados para travessia hierárquica.
    """

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

    async def save_nodes(self, nodes: List[DocumentNode]) -> None:
        """Persiste nós em lote (insert_many)."""
        if not nodes:
            return
        docs = [self._to_document(node) for node in nodes]
        try:
            await self._collection.insert_many(docs, ordered=False)
            self._logger.info("Nós salvos", count=len(docs))
        except Exception as exc:
            self._logger.error("Erro ao salvar nós", error=str(exc))
            raise

    async def get_root_nodes(self, doc_name: str) -> List[DocumentNode]:
        """Retorna nós raiz (level 0) de um documento."""
        cursor = self._collection.find(
            {"doc_name": doc_name, "level": 0}
        ).sort("_order", 1)
        return [self._to_entity(doc) async for doc in cursor]

    async def get_children(self, parent_id: str) -> List[DocumentNode]:
        """Retorna filhos diretos de um nó."""
        cursor = self._collection.find(
            {"parent_id": parent_id}
        ).sort("_order", 1)
        return [self._to_entity(doc) async for doc in cursor]

    async def get_node(self, node_id: str) -> Optional[DocumentNode]:
        """Busca um nó pelo ID."""
        doc = await self._collection.find_one({"id": node_id})
        return self._to_entity(doc) if doc else None

    async def exists(self, doc_name: str) -> bool:
        """Verifica se o documento já está indexado."""
        count = await self._collection.count_documents(
            {"doc_name": doc_name}, limit=1
        )
        return count > 0

    # ── mappers ─────────────────────────────────────────────────────

    @staticmethod
    def _to_document(node: DocumentNode) -> dict:
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
        }

    @staticmethod
    def _to_entity(data: dict) -> DocumentNode:
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
