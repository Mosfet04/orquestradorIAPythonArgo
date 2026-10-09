"""Repositório de AgentConfig — MongoDB async (motor)."""

from __future__ import annotations

from typing import List

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.rag_config import (
    DEFAULT_EMBEDDER_MODEL,
    DEFAULT_EMBEDDER_PROVIDER,
    RagConfig,
    SearchStrategy,
)
from src.domain.ports import ILogger
from src.domain.repositories.agent_config_repository import IAgentConfigRepository
from src.infrastructure.repositories.mongo_base import (
    INVALID_DOCUMENT_ERRORS,
    AsyncMongoRepository,
)


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
            # Ordem determinística (o documento mais antigo primeiro): com id repetido, o
            # use case fica com o primeiro.
            cursor = self._collection.find({"active": True}).sort("_id", 1)
            configs: List[AgentConfig] = []
            async for doc in cursor:
                self._warn_ignored_camel_case("agent_id", doc)
                # Documento inválido isola só ele: o startup segue com os válidos (F1-10).
                try:
                    configs.append(self._map_to_entity(doc))
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
            doc = await self._collection.find_one({"id": agent_id})
            if not doc:
                raise ValueError(f"Agente {agent_id} não encontrado")
            self._warn_ignored_camel_case("agent_id", doc)
            return self._map_to_entity(doc)
        except Exception as exc:
            self._logger.error(
                "Erro ao buscar agente", agent_id=agent_id, error=str(exc)
            )
            raise

    @staticmethod
    def _map_rag(rag_data: dict) -> RagConfig:
        """``rag_config`` -> ``RagConfig``.

        Legado (sem ``model_params``/``base_url``/``api_key_ref``): provider/modelo ausentes
        caem nos defaults de sempre. Com algum campo novo, nada de default: endpoint e chave
        novos não podem parar num ollama/nomic implícito, então ``model`` e provider precisam
        estar no documento (senão o ``RagConfig`` recusa).
        """
        model_params = rag_data.get("model_params")
        base_url = rag_data.get("base_url")
        api_key_ref = rag_data.get("api_key_ref")
        legacy = model_params is None and base_url is None and api_key_ref is None
        return RagConfig(
            active=rag_data.get("active", False),
            doc_name=rag_data.get("doc_name"),
            model=rag_data.get("model", DEFAULT_EMBEDDER_MODEL if legacy else None),
            factory_ia_model=rag_data.get(
                "factory_ia_model",
                rag_data.get("factoryIaModel", DEFAULT_EMBEDDER_PROVIDER if legacy else None),
            ),
            search_strategy=SearchStrategy(rag_data.get("search_strategy", "semantic")),
            model_params=model_params,
            base_url=base_url,
            api_key_ref=api_key_ref,
        )

    @staticmethod
    def _map_to_entity(data: dict) -> AgentConfig:
        rag_data = data.get("rag_config")
        rag_config = (
            MongoAgentConfigRepository._map_rag(rag_data) if rag_data else None
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
            model_params=data.get("model_params"),
            base_url=data.get("base_url"),
            api_key_ref=data.get("api_key_ref"),
        )
