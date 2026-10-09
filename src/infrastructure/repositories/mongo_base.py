"""Cliente MongoDB async compartilhado (motor)."""

from __future__ import annotations

import os
from collections.abc import Mapping

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection

from src.domain.ports import ILogger


class MongoClientFactory:
    """Gerencia uma única instância de AsyncIOMotorClient por connection string."""

    _instances: dict[str, AsyncIOMotorClient] = {}

    @classmethod
    def get_client(cls, connection_string: str) -> AsyncIOMotorClient:
        if connection_string not in cls._instances:
            use_tls = "mongodb.net" in connection_string or (
                os.getenv("USE_TLS", "false").lower() == "true"
            )
            tls_insecure = os.getenv(
                "TLS_ALLOW_INVALID_CERTIFICATES", "false"
            ).lower() == "true"

            opts: dict = {
                "serverSelectionTimeoutMS": 30_000,
                "connectTimeoutMS": 30_000,
                "socketTimeoutMS": 30_000,
                "maxPoolSize": 50,
                "minPoolSize": 1,
                "maxIdleTimeMS": 30_000,
            }
            if use_tls:
                opts.update(
                    tls=True,
                    tlsAllowInvalidCertificates=tls_insecure,
                    tlsAllowInvalidHostnames=tls_insecure,
                    retryWrites=True,
                    w="majority",
                )

            cls._instances[connection_string] = AsyncIOMotorClient(
                connection_string, **opts
            )
        return cls._instances[connection_string]


# Erros do mapeamento documento -> entidade (campo ausente, tipo errado, valor fora do enum).
INVALID_DOCUMENT_ERRORS = (ValueError, TypeError, AttributeError, KeyError)

# Grafia camelCase dos campos novos (F2-01): o mapper só lê snake_case; estas são ignoradas.
IGNORED_CAMEL_CASE_KEYS = ("apiKeyRef", "baseUrl", "modelParams")


class AsyncMongoRepository:
    """Base para repositórios MongoDB async."""

    def __init__(
        self,
        *,
        connection_string: str,
        database_name: str,
        collection_name: str,
        logger: ILogger,
    ) -> None:
        self._logger = logger
        self._client = MongoClientFactory.get_client(connection_string)
        self._db = self._client[database_name]
        self._collection: AsyncIOMotorCollection = self._db[collection_name]

    def _log_invalid_document(
        self, message: str, id_field: str, doc: Mapping[str, object], exc: Exception
    ) -> None:
        """Log de documento ignorado: id (se for texto), ``_id`` (se ObjectId) e tipo do erro.

        Nunca o documento nem o texto do erro: config pode carregar segredo ou PII.
        """
        raw_id = doc.get("id")
        mongo_id = doc.get("_id")
        self._logger.error(
            message,
            **{id_field: raw_id if isinstance(raw_id, str) else None},
            mongo_id=str(mongo_id) if isinstance(mongo_id, ObjectId) else None,
            error_type=type(exc).__name__,
        )

    def _warn_ignored_camel_case(self, id_field: str, doc: Mapping[str, object]) -> None:
        """Aviso quando o documento usa ``apiKeyRef``/``baseUrl``/``modelParams`` (raiz ou ``rag_config``).

        As chaves seguem ignoradas (o documento carrega como antes), mas quem gravou achando que
        configurou endpoint/chave precisa saber. Cita só o id e os nomes das chaves, nunca valores.
        """
        rag = doc.get("rag_config")
        keys = [key for key in IGNORED_CAMEL_CASE_KEYS if key in doc]
        rag_keys = [key for key in IGNORED_CAMEL_CASE_KEYS if isinstance(rag, Mapping) and key in rag]
        if not keys and not rag_keys:
            return
        raw_id = doc.get("id")
        mongo_id = doc.get("_id")
        self._logger.warning(
            "Documento com chaves camelCase ignoradas; use model_params, base_url e api_key_ref",
            **{id_field: raw_id if isinstance(raw_id, str) else None},
            mongo_id=str(mongo_id) if isinstance(mongo_id, ObjectId) else None,
            keys=keys,
            rag_config_keys=rag_keys,
        )

    async def ping(self) -> bool:
        """Verifica conectividade."""
        try:
            await self._client.admin.command("ping")
            return True
        except Exception as exc:
            self._logger.error("MongoDB ping falhou", error=str(exc))
            return False
