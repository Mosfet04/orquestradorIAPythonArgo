"""Cliente MongoDB async compartilhado (motor)."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any, ClassVar

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection

from src.domain.ports import ILogger
from src.infrastructure.repositories.config_documents import (
    log_invalid_document,
    warn_ignored_camel_case,
)

# Documento cru do Mongo (sem schema).
MongoDocument = dict[str, Any]


class MongoClientFactory:
    """Gerencia uma única instância de AsyncIOMotorClient por connection string."""

    _instances: ClassVar[dict[str, AsyncIOMotorClient[MongoDocument]]] = {}

    @classmethod
    def get_client(cls, connection_string: str) -> AsyncIOMotorClient[MongoDocument]:
        if connection_string not in cls._instances:
            use_tls = "mongodb.net" in connection_string or (
                os.getenv("USE_TLS", "false").lower() == "true"
            )
            tls_insecure = os.getenv(
                "TLS_ALLOW_INVALID_CERTIFICATES", "false"
            ).lower() == "true"

            opts: dict[str, Any] = {
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
        self._collection: AsyncIOMotorCollection[MongoDocument] = self._db[collection_name]

    # Ordem estável dos documentos de config (F2-06): o mais antigo primeiro. A busca por id usa a
    # mesma ordem, então com id repetido acha o mesmo documento que a listagem põe na frente.
    _STABLE_ORDER: ClassVar[list[tuple[str, int]]] = [("_id", 1)]

    @staticmethod
    def _location(doc: object) -> dict[str, object]:
        """Onde o documento está no Mongo: o ``_id``, se for ObjectId."""
        mongo_id = doc.get("_id") if isinstance(doc, Mapping) else None
        return {"mongo_id": str(mongo_id) if isinstance(mongo_id, ObjectId) else None}

    def _log_invalid_document(self, message: str, id_field: str, doc: object, exc: Exception) -> None:
        """Log de documento ignorado (id, ``mongo_id`` e tipo do erro; nunca o documento)."""
        log_invalid_document(self._logger, message, id_field, doc, exc, self._location(doc))

    def _warn_ignored_camel_case(self, id_field: str, doc: object) -> None:
        """Aviso de ``apiKeyRef``/``baseUrl``/``modelParams`` ignoradas (só id e nomes das chaves)."""
        warn_ignored_camel_case(self._logger, id_field, doc, self._location(doc))

    async def ping(self) -> bool:
        """Verifica conectividade."""
        try:
            await self._client.admin.command("ping")
            return True
        except Exception as exc:  # noqa: BLE001 - ping devolve False; falha logada
            self._logger.error("MongoDB ping falhou", error_type=type(exc).__name__)
            return False
