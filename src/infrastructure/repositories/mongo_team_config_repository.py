"""Repositório de TeamConfig — MongoDB async (motor)."""

from __future__ import annotations

from src.domain.entities.team_config import TeamConfig
from src.domain.ports import ILogger
from src.domain.repositories.team_config_repository import ITeamConfigRepository
from src.infrastructure.repositories.config_documents import map_team_document
from src.infrastructure.repositories.mongo_base import MongoConfigRepository, MongoDocument


class MongoTeamConfigRepository(MongoConfigRepository[TeamConfig], ITeamConfigRepository):
    """Implementação async do repositório de configurações de teams."""

    _id_field = "team_id"
    _invalid_message = "Documento de team inválido ignorado"

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
        """Lista os teams ativos, do documento mais antigo ao mais novo."""
        try:
            # Ordem determinística (o documento mais antigo primeiro): com id repetido, o
            # use case fica com o primeiro. Documento inválido isola só ele (F1-10).
            return await self._documents({"active": True}, self._map)
        except Exception as exc:
            self._logger.error("Erro ao buscar teams ativos", error_type=type(exc).__name__)
            raise

    def _map(self, doc: MongoDocument) -> TeamConfig:
        return map_team_document(doc, self._logger)

    async def get_team_by_id(self, team_id: str) -> TeamConfig | None:
        """Devolve o team pelo id, ou ``None`` se não existir."""
        try:
            doc = await self._collection.find_one({"id": team_id}, sort=self._STABLE_ORDER)
            if not doc:
                return None
            self._warn_ignored_camel_case("team_id", doc)
            return map_team_document(doc, self._logger)
        except Exception as exc:
            self._logger.error(
                "Erro ao buscar team", team_id=team_id, error_type=type(exc).__name__
            )
            raise
