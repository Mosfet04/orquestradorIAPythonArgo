"""Repositório de Tools — MongoDB async (motor)."""

from __future__ import annotations

from typing import List

from src.domain.entities.tool import HttpMethod, ParameterType, Tool, ToolParameter
from src.domain.ports import ILogger
from src.domain.repositories.tool_repository import IToolRepository
from src.infrastructure.repositories.mongo_base import AsyncMongoRepository


class MongoToolRepository(AsyncMongoRepository, IToolRepository):
    """Implementação async do repositório de tools."""

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

    async def get_tools_by_ids(self, tool_ids: List[str]) -> List[Tool]:
        if not tool_ids:
            return []
        try:
            cursor = self._collection.find(
                {"id": {"$in": tool_ids}, "active": True}
            )
            return [self._map_to_entity(doc) async for doc in cursor]
        except Exception as exc:
            self._logger.error(
                "Erro ao buscar tools por IDs", tool_ids=tool_ids, error=str(exc)
            )
            raise

    async def get_tool_by_id(self, tool_id: str) -> Tool:
        try:
            doc = await self._collection.find_one({"id": tool_id})
            if not doc:
                raise ValueError(f"Tool {tool_id} não encontrada")
            return self._map_to_entity(doc)
        except Exception as exc:
            self._logger.error(
                "Erro ao buscar tool", tool_id=tool_id, error=str(exc)
            )
            raise

    async def get_all_active_tools(self) -> List[Tool]:
        try:
            cursor = self._collection.find({"active": True})
            return [self._map_to_entity(doc) async for doc in cursor]
        except Exception as exc:
            self._logger.error("Erro ao listar tools ativas", error=str(exc))
            raise

    @staticmethod
    def _map_to_entity(data: dict) -> Tool:
        parameters: List[ToolParameter] = []
        for p in data.get("parameters", []):
            raw_type = p.get("type")
            try:
                ptype = ParameterType(raw_type)
            except (ValueError, KeyError):
                ptype = ParameterType[raw_type.upper()] if isinstance(raw_type, str) else ParameterType.STRING
            parameters.append(
                ToolParameter(
                    name=p.get("name"),
                    type=ptype,
                    description=p.get("description"),
                    required=p.get("required", False),
                    default_value=p.get("default_value"),
                )
            )

        raw_method = data.get("http_method", "GET")
        try:
            method = HttpMethod(raw_method)
        except (ValueError, KeyError):
            method = HttpMethod[raw_method.upper()] if isinstance(raw_method, str) else HttpMethod.GET

        return Tool(
            id=data.get("id", ""),
            name=data.get("name", ""),
            description=data.get("description", ""),
            route=data.get("route", ""),
            http_method=method,
            parameters=parameters,
            instructions=data.get("instructions", ""),
            headers=data.get("headers", {}),
            active=data.get("active", True),
        )
