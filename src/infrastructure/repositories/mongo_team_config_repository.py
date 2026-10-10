"""Repositório de TeamConfig — MongoDB async (motor)."""

from __future__ import annotations

from src.domain.entities.team_config import TeamConfig
from src.domain.ports import ILogger
from src.domain.repositories.team_config_repository import ITeamConfigRepository
from src.infrastructure.repositories.config_documents import (
    INVALID_DOCUMENT_ERRORS,
    map_team_document,
)
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

    async def get_active_teams(self) -> list[TeamConfig]:
        try:
            # Ordem determinística (o documento mais antigo primeiro): com id repetido, o
            # use case fica com o primeiro.
            cursor = self._collection.find({"active": True}).sort(self._STABLE_ORDER)
            configs: list[TeamConfig] = []
            async for doc in cursor:
                self._warn_ignored_camel_case("team_id", doc)
                # Documento inválido isola só ele: o startup segue com os válidos (F1-10).
                try:
                    configs.append(map_team_document(doc, self._logger))
                except INVALID_DOCUMENT_ERRORS as exc:
                    self._log_invalid_document(
                        "Documento de team inválido ignorado", "team_id", doc, exc
                    )
            return configs
        except Exception as exc:
            self._logger.error("Erro ao buscar teams ativos", error=str(exc))
            raise

    async def get_team_by_id(self, team_id: str) -> TeamConfig | None:
        try:
            doc = await self._collection.find_one({"id": team_id}, sort=self._STABLE_ORDER)
            if not doc:
                return None
            self._warn_ignored_camel_case("team_id", doc)
            return map_team_document(doc, self._logger)
        except Exception as exc:
            self._logger.error(
                "Erro ao buscar team", team_id=team_id, error=str(exc)
            )
            raise
