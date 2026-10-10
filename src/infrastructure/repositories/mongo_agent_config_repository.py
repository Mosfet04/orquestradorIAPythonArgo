"""Repositório de AgentConfig — MongoDB async (motor)."""

from __future__ import annotations

from src.domain.entities.agent_config import AgentConfig
from src.domain.ports import ILogger
from src.domain.repositories.agent_config_repository import IAgentConfigRepository
from src.infrastructure.repositories.config_documents import (
    INVALID_DOCUMENT_ERRORS,
    map_agent_document,
)
from src.infrastructure.repositories.mongo_base import AsyncMongoRepository


class MongoAgentConfigRepository(AsyncMongoRepository, IAgentConfigRepository):
    """Implementação async do repositório de configurações de agentes."""

    def __init__(
        self,
        *,
        connection_string: str,
        database_name: str = "agno",
        collection_name: str = "agents_config",
        logger: ILogger,
    ) -> None:
        super().__init__(
            connection_string=connection_string,
            database_name=database_name,
            collection_name=collection_name,
            logger=logger,
        )

    async def get_active_agents(self) -> list[AgentConfig]:
        try:
            # Ordem determinística (o documento mais antigo primeiro): com id repetido, o
            # use case fica com o primeiro.
            cursor = self._collection.find({"active": True}).sort(self._STABLE_ORDER)
            configs: list[AgentConfig] = []
            async for doc in cursor:
                self._warn_ignored_camel_case("agent_id", doc)
                # Documento inválido isola só ele: o startup segue com os válidos (F1-10).
                try:
                    configs.append(map_agent_document(doc, self._logger))
                except INVALID_DOCUMENT_ERRORS as exc:
                    self._log_invalid_document(
                        "Documento de agente inválido ignorado", "agent_id", doc, exc
                    )
            return configs
        except Exception as exc:
            self._logger.error("Erro ao buscar agentes ativos", error=str(exc))
            raise

    async def get_agent_by_id(self, agent_id: str) -> AgentConfig:
        try:
            doc = await self._collection.find_one({"id": agent_id}, sort=self._STABLE_ORDER)
            if not doc:
                raise ValueError(f"Agente {agent_id} não encontrado")
            self._warn_ignored_camel_case("agent_id", doc)
            return map_agent_document(doc, self._logger)
        except Exception as exc:
            self._logger.error(
                "Erro ao buscar agente", agent_id=agent_id, error=str(exc)
            )
            raise
