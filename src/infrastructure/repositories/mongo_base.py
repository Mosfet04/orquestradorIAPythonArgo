"""Cliente MongoDB async compartilhado (motor)."""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, ClassVar, Generic, TypeVar

from bson import ObjectId
from motor.motor_asyncio import AsyncIOMotorClient, AsyncIOMotorCollection
from pymongo.errors import DuplicateKeyError

from src.domain.ports import ILogger
from src.domain.repositories.versioning import ConfigVersionConflict, Versioned
from src.infrastructure.repositories.config_documents import (
    INVALID_DOCUMENT_ERRORS,
    ConfigEntityT,
    document_version,
    entity_document,
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
        """Devolve o cliente compartilhado da connection string, criando-o na primeira chamada."""
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


T = TypeVar("T")


def _at_version(entity_id: str, version: int) -> MongoDocument:
    """Filtro do documento do id na versão; a 0 inclui o legado, sem ``version``.

    Só texto e inteiro chegam ao filtro: um id ou versão operador (``{"$ne": ""}``) casaria o
    documento de outro id (injeção NoSQL).
    """
    if not isinstance(entity_id, str) or isinstance(version, bool) or not isinstance(version, int):
        raise TypeError("id da config deve ser texto e a versão, inteiro")
    return {"id": entity_id, "version": {"$in": [0, None]} if version == 0 else version}


class MongoConfigRepository(AsyncMongoRepository, ABC, Generic[ConfigEntityT]):
    """Base dos repositórios de config: leitura isolando documento inválido e escrita otimista (FA-01).

    Concorrência otimista por ``version``: a escrita substitui o documento inteiro (mesmo ``_id``)
    pelo da entidade, em snake_case, com ``version`` e ``updated_at``; o legado camelCase e campo
    fora da entidade deixam de existir no primeiro ``save``. Toda substituição é condicional à
    versão e atômica no documento; só a criação de um id novo (``expected_version`` 0 sem documento)
    é verificar-e-inserir: duas criações simultâneas do mesmo id gravariam as duas se a coleção não
    tiver o índice único de ``id`` do seed (``mongo-init/init-db.js``); com ele, a segunda conflita.
    """

    _id_field: ClassVar[str]
    _invalid_message: ClassVar[str]
    # Aviso de apiKeyRef/baseUrl/modelParams ignoradas: só agentes e teams têm modelo.
    _warns_camel_case: ClassVar[bool] = True

    @abstractmethod
    def _map(self, doc: MongoDocument) -> ConfigEntityT:
        """Documento -> entidade (o ``map_*_document`` do tipo)."""

    async def _documents(self, query: MongoDocument, mapper: Callable[[MongoDocument], T]) -> list[T]:
        """Documentos da consulta mapeados, na ordem estável; documento inválido isola só ele (F1-10)."""
        result: list[T] = []
        async for doc in self._collection.find(query).sort(self._STABLE_ORDER):
            if self._warns_camel_case:
                self._warn_ignored_camel_case(self._id_field, doc)
            try:
                result.append(mapper(doc))
            except INVALID_DOCUMENT_ERRORS as exc:
                self._log_invalid_document(self._invalid_message, self._id_field, doc, exc)
        return result

    async def list_all(self) -> list[Versioned[ConfigEntityT]]:
        """Todos os documentos, inclusive inativos e ids repetidos, na ordem estável, com a versão."""
        try:
            return await self._documents({}, lambda doc: Versioned(self._map(doc), document_version(doc)))
        except Exception as exc:
            self._logger.error("Erro ao listar documentos de config", error_type=type(exc).__name__)
            raise

    async def save(self, entity: ConfigEntityT, expected_version: int) -> int:
        """Substitui o documento do id se ele estiver em ``expected_version``; com 0, cria se não existir."""
        query = _at_version(entity.id, expected_version)
        version = expected_version + 1
        document = {**entity_document(entity), "version": version, "updated_at": datetime.now(UTC)}
        if (await self._collection.replace_one(query, document)).matched_count:
            return version
        if expected_version != 0 or await self._collection.find_one({"id": entity.id}, {"_id": 1}):
            raise ConfigVersionConflict(entity.id, expected_version)
        try:
            await self._collection.insert_one(document)
        except DuplicateKeyError:
            raise ConfigVersionConflict(entity.id, expected_version) from None
        return version

    async def delete(self, entity_id: str, expected_version: int) -> None:
        """Remove o documento do id se ele estiver em ``expected_version``."""
        result = await self._collection.delete_one(_at_version(entity_id, expected_version))
        if result.deleted_count == 0:
            raise ConfigVersionConflict(entity_id, expected_version)
