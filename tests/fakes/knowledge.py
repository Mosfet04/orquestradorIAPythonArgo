"""Knowledge/vector db do agno sem Mongo: corta o I/O na borda e registra o que chegaria nela.

``cut_agno_io`` é o corte único (agno 2.5.8), usado pelo ``offline_knowledge`` daqui e pelo
``agno_calls`` dos golden tests:
- ``MongoClient`` do ``MongoDb`` de sessões com ``connect=False`` (nada abre socket);
- ``MongoVectorDb.exists`` responde ``True`` (o ``Knowledge.__post_init__`` não cria
  coleção/índice) e ``MongoVectorDb.create`` falha alto se chamado;
- ``Knowledge.insert`` só chama o gancho (sem ler arquivo, gerar embedding nem escrever).
"""

from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import agno.db.mongo.mongo as agno_mongo_db
import pytest
from agno.knowledge import Knowledge
from agno.vectordb.mongodb import MongoDb as MongoVectorDb
from pymongo import MongoClient

from tests.fakes.models import running_on_event_loop

InsertHook = Callable[[Knowledge, tuple[Any, ...], dict[str, Any]], None]
"""Recebe ``(knowledge, args, kwargs)`` de cada ``Knowledge.insert``."""


@contextmanager
def cut_agno_io(
    monkeypatch: pytest.MonkeyPatch,
    *,
    on_insert: InsertHook,
    on_exists: Callable[[], None] | None = None,
) -> Iterator[None]:
    """Corta o I/O do agno na borda enquanto o bloco roda; fecha os clientes criados no fim."""
    clients: list[MongoClient[Any]] = []

    def lazy_client(*args: Any, **kwargs: Any) -> MongoClient[Any]:
        client: MongoClient[Any] = MongoClient(*args, connect=False, **kwargs)
        clients.append(client)
        return client

    def exists(self: MongoVectorDb) -> bool:
        if on_exists is not None:
            on_exists()
        return True

    def refuse_create(self: MongoVectorDb) -> None:
        raise AssertionError("cut_agno_io: MongoVectorDb.create não deveria ser chamado")

    def insert(self: Knowledge, *args: Any, **kwargs: Any) -> None:
        on_insert(self, args, kwargs)

    monkeypatch.setattr(agno_mongo_db, "MongoClient", lazy_client)
    monkeypatch.setattr(MongoVectorDb, "exists", exists)
    monkeypatch.setattr(MongoVectorDb, "create", refuse_create)
    monkeypatch.setattr(Knowledge, "insert", insert)
    try:
        yield
    finally:
        for client in clients:
            client.close()


@dataclass(frozen=True)
class KnowledgeInsert:
    collection_name: str | None
    path: str | None
    skip_if_exists: bool | None
    on_event_loop: bool


@dataclass
class OfflineKnowledge:
    inserts: list[KnowledgeInsert] = field(default_factory=list)
    exists_on_event_loop: list[bool] = field(default_factory=list)


@pytest.fixture
def offline_knowledge(monkeypatch: pytest.MonkeyPatch) -> Iterator[OfflineKnowledge]:
    """Cada registro diz se a chamada rodou na thread do event loop (I/O síncrono no caminho async)."""
    recorded = OfflineKnowledge()

    def record_insert(knowledge: Knowledge, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        if args:
            raise AssertionError(f"Knowledge.insert com argumentos posicionais: {args!r}")
        recorded.inserts.append(
            KnowledgeInsert(
                collection_name=getattr(knowledge.vector_db, "collection_name", None),
                path=kwargs.get("path"),
                skip_if_exists=kwargs.get("skip_if_exists"),
                on_event_loop=running_on_event_loop(),
            )
        )

    def record_exists() -> None:
        recorded.exists_on_event_loop.append(running_on_event_loop())

    with cut_agno_io(monkeypatch, on_insert=record_insert, on_exists=record_exists):
        yield recorded
