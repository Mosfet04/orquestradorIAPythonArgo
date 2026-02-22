"""Repositório de AgentConfig — MongoDB async (motor)."""

from __future__ import annotations

from typing import List

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.rag_config import RagConfig
from src.domain.ports import ILogger
from src.domain.repositories.agent_config_repository import IAgentConfigRepository
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

    async def get_active_agents(self) -> List[AgentConfig]:
        try:
            cursor = self._collection.find({"active": True})
            return [self._map_to_entity(doc) async for doc in cursor]
        except Exception as exc:
            self._logger.error("Erro ao buscar agentes ativos", error=str(exc))
            raise

    async def get_agent_by_id(self, agent_id: str) -> AgentConfig:
        try:
            doc = await self._collection.find_one({"id": agent_id})
            if not doc:
                raise ValueError(f"Agente {agent_id} não encontrado")
            return self._map_to_entity(doc)
        except Exception as exc:
            self._logger.error(
                "Erro ao buscar agente", agent_id=agent_id, error=str(exc)
            )
            raise

    @staticmethod
    def _map_to_entity(data: dict) -> AgentConfig:
        rag_data = data.get("rag_config")
        rag_config = (
            RagConfig(
                active=rag_data.get("active", False),
                doc_name=rag_data.get("doc_name"),
                model=rag_data.get("model", "nomic-embed-text:latest"),
                factory_ia_model=rag_data.get(
                    "factory_ia_model",
                    rag_data.get("factoryIaModel", "ollama"),
                ),
            )
            if rag_data
            else None
        )

        return AgentConfig(
            id=data.get("id", ""),
            nome=data.get("nome", ""),
            model=data.get("model", ""),
            factory_ia_model=data.get(
                "factory_ia_model",
                data.get("factoryIaModel", "ollama"),
            ),
            descricao=data.get("descricao", ""),
            prompt=data.get("prompt", ""),
            active=data.get("active", True),
            tools_ids=data.get("tools_ids", []),
            rag_config=rag_config,
            user_memory_active=data.get("user_memory_active", False),
            summary_active=data.get("summary_active", False),
        )
