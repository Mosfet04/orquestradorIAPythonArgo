"""Repositório de Tools — MongoDB async (motor)."""

from __future__ import annotations

from src.domain.entities.tool import Tool
from src.domain.ports import ILogger
from src.domain.repositories.tool_repository import IToolRepository
from src.infrastructure.repositories.config_documents import first_tool_per_id, map_tool_document
from src.infrastructure.repositories.mongo_base import MongoConfigRepository, MongoDocument


class MongoToolRepository(MongoConfigRepository[Tool], IToolRepository):
    """Implementação async do repositório de tools."""

    _id_field = "tool_id"
    # quem a referencia loga a ausência com o id do agente (AgentFactoryService)
    _invalid_message = "Documento de tool inválido ignorado"
    _warns_camel_case = False

    def __init__(
        self,
        *,
        connection_string: str,
        database_name: str = "agno",
        collection_name: str = "tools",
        logger: ILogger,
    ) -> None:
        super().__init__(
            connection_string=connection_string,
            database_name=database_name,
            collection_name=collection_name,
            logger=logger,
        )

    async def get_tools_by_ids(self, tool_ids: list[str]) -> list[Tool]:
        """Devolve as tools ativas com os ids pedidos."""
        if not tool_ids:
            return []
        try:
            return await self._active_tools({"id": {"$in": tool_ids}, "active": True})
        except Exception as exc:
            self._logger.error(
                "Erro ao buscar tools por IDs", tool_ids=tool_ids, error_type=type(exc).__name__
            )
            raise

    async def get_tool_by_id(self, tool_id: str) -> Tool:
        """Devolve a tool pelo id; ``ValueError`` se não existir."""
        try:
            doc = await self._collection.find_one({"id": tool_id}, sort=self._STABLE_ORDER)
            if not doc:
                raise ValueError(f"Tool {tool_id} não encontrada")
            return map_tool_document(doc)
        except Exception as exc:
            self._logger.error(
                "Erro ao buscar tool", tool_id=tool_id, error_type=type(exc).__name__
            )
            raise

    async def get_all_active_tools(self) -> list[Tool]:
        """Lista as tools ativas."""
        try:
            return await self._active_tools({"active": True})
        except Exception as exc:
            self._logger.error("Erro ao listar tools ativas", error_type=type(exc).__name__)
            raise

    def _map(self, doc: MongoDocument) -> Tool:
        return map_tool_document(doc)

    async def _active_tools(self, query: MongoDocument) -> list[Tool]:
        """Tools da consulta na ordem estável; inválida isola só ela e o id repetido fica com a primeira."""
        return first_tool_per_id(await self._documents(query, self._map), self._logger)
