"""Repositório de TeamConfig — MongoDB async (motor)."""

from __future__ import annotations

from typing import List, Optional

from src.domain.entities.team_config import TeamConfig
from src.domain.ports import ILogger
from src.domain.repositories.team_config_repository import ITeamConfigRepository
from src.infrastructure.repositories.mongo_base import AsyncMongoRepository


class MongoTeamConfigRepository(AsyncMongoRepository, ITeamConfigRepository):
    """Implementação async do repositório de configurações de teams."""

    def __init__(
        self,
        *,
        connection_string: str,
        database_name: str = "agno",
        collection_name: str = "teams_config",
        logger: ILogger,
    ) -> None:
        super().__init__(
            connection_string=connection_string,
            database_name=database_name,
            collection_name=collection_name,
            logger=logger,
        )

    async def get_active_teams(self) -> List[TeamConfig]:
        try:
            cursor = self._collection.find({"active": True})
            return [self._map_to_entity(doc) async for doc in cursor]
        except Exception as exc:
            self._logger.error("Erro ao buscar teams ativos", error=str(exc))
            raise

    async def get_team_by_id(self, team_id: str) -> Optional[TeamConfig]:
        try:
            doc = await self._collection.find_one({"id": team_id})
            if not doc:
                return None
            return self._map_to_entity(doc)
        except Exception as exc:
            self._logger.error(
                "Erro ao buscar team", team_id=team_id, error=str(exc)
            )
            raise

    @staticmethod
    def _map_to_entity(data: dict) -> TeamConfig:
        return TeamConfig(
            id=data.get("id", ""),
            nome=data.get("nome", ""),
            model=data.get("model", ""),
            factory_ia_model=data.get(
                "factory_ia_model",
                data.get("factoryIaModel", "ollama"),
            ),
            mode=data.get("mode", "route"),
            descricao=data.get("descricao"),
            prompt=data.get("prompt"),
            member_ids=data.get(
                "member_ids",
                data.get("memberIds", []),
            ),
            user_memory_active=data.get(
                "user_memory_active",
                data.get("userMemoryActive", True),
            ),
            summary_active=data.get(
                "summary_active",
                data.get("summaryActive", False),
            ),
            active=data.get("active", True),
        )
