"""Mongo real para os testes ``live`` (marker ``live``; nunca na CI padrão).

``MONGO_LIVE_URL`` é credencial interna: só vem do ambiente e nunca vai para log, id de teste,
mensagem de asserção ou trace (o ``LiveMongo`` a deixa fora do ``repr``; não a ponha em
``parametrize`` nem em ``assert``). Sem a variável o teste é pulado. Cada teste recebe um banco
efêmero de nome único, apagado no teardown, e nada mais é tocado no servidor.
"""

from __future__ import annotations

import os
import uuid
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pytest
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import MongoClient

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
