"""Repositório de AgentConfig — MongoDB async (motor)."""

from __future__ import annotations

from src.domain.entities.agent_config import AgentConfig
from src.domain.ports import ILogger
from src.domain.repositories.agent_config_repository import IAgentConfigRepository
from src.infrastructure.repositories.config_documents import map_agent_document
from src.infrastructure.repositories.mongo_base import MongoConfigRepository, MongoDocument


class MongoAgentConfigRepository(MongoConfigRepository[AgentConfig], IAgentConfigRepository):
    """Implementação async do repositório de configurações de agentes."""

    _id_field = "agent_id"
    _invalid_message = "Documento de agente inválido ignorado"

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
        """Lista os agentes ativos, do documento mais antigo ao mais novo."""
        try:
            # Ordem determinística (o documento mais antigo primeiro): com id repetido, o
            # use case fica com o primeiro. Documento inválido isola só ele (F1-10).
            return await self._documents({"active": True}, self._map)
        except Exception as exc:
            self._logger.error("Erro ao buscar agentes ativos", error_type=type(exc).__name__)
            raise

    def _map(self, doc: MongoDocument) -> AgentConfig:
        return map_agent_document(doc, self._logger)

    async def get_agent_by_id(self, agent_id: str) -> AgentConfig:
        """Devolve o agente pelo id; ``ValueError`` se não existir."""
        try:
            doc = await self._collection.find_one({"id": agent_id}, sort=self._STABLE_ORDER)
            if not doc:
                raise ValueError(f"Agente {agent_id} não encontrado")
            self._warn_ignored_camel_case("agent_id", doc)
            return map_agent_document(doc, self._logger)
        except Exception as exc:
            self._logger.error(
                "Erro ao buscar agente", agent_id=agent_id, error_type=type(exc).__name__
            )
            raise
