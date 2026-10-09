"""Repositório de TeamConfig — MongoDB async (motor)."""

from __future__ import annotations

from typing import List, Optional

from src.domain.entities.team_config import TeamConfig
from src.domain.ports import ILogger
from src.domain.repositories.team_config_repository import ITeamConfigRepository
from src.infrastructure.repositories.mongo_base import (
    INVALID_DOCUMENT_ERRORS,
    AsyncMongoRepository,
)


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
            # Ordem determinística (o documento mais antigo primeiro): com id repetido, o
            # use case fica com o primeiro.
            cursor = self._collection.find({"active": True}).sort("_id", 1)
            configs: List[TeamConfig] = []
            async for doc in cursor:
                self._warn_ignored_camel_case("team_id", doc)
                # Documento inválido isola só ele: o startup segue com os válidos (F1-10).
                try:
                    configs.append(self._map_to_entity(doc))
                except INVALID_DOCUMENT_ERRORS as exc:
                    self._log_invalid_document(
                        "Documento de team inválido ignorado", "team_id", doc, exc
                    )
            return configs
        except Exception as exc:
            self._logger.error("Erro ao buscar teams ativos", error=str(exc))
            raise

    async def get_team_by_id(self, team_id: str) -> Optional[TeamConfig]:
        try:
            doc = await self._collection.find_one({"id": team_id})
            if not doc:
                return None
            self._warn_ignored_camel_case("team_id", doc)
            return self._map_to_entity(doc)
        except Exception as exc:
            self._logger.error(
                "Erro ao buscar team", team_id=team_id, error=str(exc)
            )
            raise

    @staticmethod
    def _map_to_entity(data: dict) -> TeamConfig:
        """Documento -> ``TeamConfig``.

        Canônico (README e seed): ``factoryIaModel`` e o resto em snake_case
        (``member_ids``, ``user_memory_active``, ``summary_active``). Ainda lê o legado
        camelCase (``memberIds``, ``userMemoryActive``, ``summaryActive``) e
        ``factory_ia_model``; com as duas grafias no documento, vale a snake_case.
        Opcionais (F2-01, só snake_case): ``model_params``, ``base_url``, ``api_key_ref``.
        """
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
            model_params=data.get("model_params"),
            base_url=data.get("base_url"),
            api_key_ref=data.get("api_key_ref"),
        )
