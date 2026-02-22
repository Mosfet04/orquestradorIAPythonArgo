"""Cliente MongoDB async compartilhado (motor)."""

from __future__ import annotations

import os
from typing import Optional

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

    async def ping(self) -> bool:
        """Verifica conectividade."""
        try:
            await self._client.admin.command("ping")
            return True
        except Exception as exc:
            self._logger.error("MongoDB ping falhou", error=str(exc))
            return False
