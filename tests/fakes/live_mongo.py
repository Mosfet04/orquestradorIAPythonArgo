"""Mongo real para os testes ``live`` (marker ``live``; nunca na CI padrão).

``MONGO_LIVE_URL`` é credencial interna: só vem do ambiente e nunca vai para log, id de teste,
mensagem de asserção ou trace (o ``LiveMongo`` a deixa fora do ``repr``; não a ponha em
``parametrize`` nem em ``assert``). Sem a variável o teste é pulado. Cada teste recebe um banco
efêmero de nome único, apagado no teardown, e nada mais é tocado no servidor.
"""

from __future__ import annotations

import asyncio
import os
import uuid
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import AsyncMongoClient, MongoClient

LIVE_URL_ENV = "MONGO_LIVE_URL"
# Falha rápida se o servidor da URL não responder (o padrão do driver é 30 s).
_SERVER_SELECTION_TIMEOUT_MS = 5000


@dataclass
class LiveMongo:
    """Servidor Mongo real e o banco efêmero do teste."""

    url: str = field(repr=False)
    database: str
    _sync: MongoClient[dict[str, Any]] | None = field(default=None, repr=False)
    _motors: list[AsyncIOMotorClient[dict[str, Any]]] = field(default_factory=list, repr=False)
    _natives: list[AsyncMongoClient[dict[str, Any]]] = field(default_factory=list, repr=False)

    def sync(self) -> MongoClient[dict[str, Any]]:
        """Cliente pymongo síncrono (um por teste): para semear e conferir o que ficou gravado."""
        if self._sync is None:
            self._sync = MongoClient(self.url, serverSelectionTimeoutMS=_SERVER_SELECTION_TIMEOUT_MS)
        return self._sync

    def motor(self) -> AsyncIOMotorClient[dict[str, Any]]:
        """Cliente motor novo (fechado no teardown): crie e use dentro do event loop do teste."""
        client: AsyncIOMotorClient[dict[str, Any]] = AsyncIOMotorClient(
            self.url, serverSelectionTimeoutMS=_SERVER_SELECTION_TIMEOUT_MS
        )
        self._motors.append(client)
        return client

    def native(self) -> AsyncMongoClient[dict[str, Any]]:
        """``AsyncMongoClient`` novo (F3-09), preso ao event loop do teste: peça a fixture
        ``live_native_closed``, que o fecha com ``await`` nesse loop antes de apagar o banco."""
        client: AsyncMongoClient[dict[str, Any]] = AsyncMongoClient(
            self.url, serverSelectionTimeoutMS=_SERVER_SELECTION_TIMEOUT_MS
        )
        self._natives.append(client)
        return client

    async def close_natives(self) -> None:
        """Fecha os clientes de ``native()`` (o ``close`` deles é corrotina).

        Teardown robusto: um ``close`` que levanta não impede o dos demais, a lista é esvaziada de
        qualquer jeito e os erros sobem no fim (um só, ou um ``BaseExceptionGroup`` se forem vários).
        """
        natives, self._natives = list(self._natives), []
        results = await asyncio.gather(*(client.close() for client in natives), return_exceptions=True)
        errors = [result for result in results if isinstance(result, BaseException)]
        if len(errors) == 1:
            raise errors[0]
        if errors:
            raise BaseExceptionGroup("falha ao fechar clientes nativos", errors)

    def collection(self, name: str) -> Any:
        """Coleção (pymongo síncrono) do banco efêmero."""
        return self.sync()[self.database][name]

    def close(self) -> None:
        """Apaga o banco efêmero e fecha os clientes criados."""
        try:
            self.sync().drop_database(self.database)
        finally:
            for client in self._motors:
                client.close()
            self.sync().close()


@pytest.fixture
def live_mongo() -> Iterator[LiveMongo]:
    """Banco efêmero no Mongo de ``MONGO_LIVE_URL``; sem a variável, o teste é pulado."""
    url = os.environ.get(LIVE_URL_ENV, "").strip()
    if not url:
        pytest.skip(f"{LIVE_URL_ENV} não definida: teste live contra Mongo real")
    live = LiveMongo(url=url, database=f"orq_live_{uuid.uuid4().hex[:16]}")
    try:
        yield live
    finally:
        live.close()


@pytest.fixture
async def live_native_closed(live_mongo: LiveMongo) -> AsyncIterator[None]:
    """Fecha, no event loop do teste, os ``AsyncMongoClient`` de ``live_mongo.native()`` (antes de o banco
    efêmero ser apagado). Pedido pelos módulos ``live`` do db de sessões (``pytestmark``)."""
    yield
    await live_mongo.close_natives()
