"""Db de sessões de produção (``SessionDb``, F2-08) sobre o cliente motor em memória.

O que roda é o ``AsyncMongoDb`` do agno 2.5.8 de verdade (consultas, documentos, índices); só o
cliente é o ``FakeMotorClient``. Um db para todas as entidades do teste e para o AgentOS, como em
produção: passe o mesmo objeto às fábricas (``session_db=``) e ao ``mount_agent_os``.
"""

from __future__ import annotations

from typing import Any

import pytest
from agno.db.schemas import UserMemory

from src.infrastructure import dependency_injection as di
from src.infrastructure.runtime.agno.session_db import SessionDb
from tests.fakes.mongo import FakeMongoCollection, FakeMotorClient

SESSION_DB_URL = "mongodb://mongo.test.invalid:27017"
SESSION_DB_NAME = "agno"


def fake_session_db(client: FakeMotorClient | None = None, *, db_name: str = SESSION_DB_NAME) -> SessionDb:
    """``SessionDb`` com o cliente dado (ou um novo, vazio)."""
    return SessionDb(client or FakeMotorClient(), db_url=SESSION_DB_URL, db_name=db_name)  # type: ignore[arg-type]


def use_fake_session_db(monkeypatch: pytest.MonkeyPatch) -> list[SessionDb]:
    """Faz o composition root (``DependencyContainer``) criar o db de sessões sobre o cliente em
    memória; devolve a lista (viva) dos dbs criados, na ordem."""
    created: list[SessionDb] = []

    def create(db_url: str, db_name: str) -> SessionDb:
        created.append(fake_session_db(db_name=db_name))
        return created[-1]

    monkeypatch.setattr(di, "create_session_db", create)
    return created


def session_db_client(db: SessionDb) -> FakeMotorClient:
    client: Any = db.db_client
    assert isinstance(client, FakeMotorClient), "fake_session_db: o cliente do db não é o FakeMotorClient"
    return client


def collection(db: SessionDb, name: str) -> FakeMongoCollection:
    """Coleção do banco do db (``agno_sessions``, ``agno_memories``...)."""
    return session_db_client(db)[db.db_name][name]


async def seed_memories(db: SessionDb, memory_by_user: dict[str, str]) -> None:
    """Grava uma memória por usuário, pelo caminho de produção (``upsert_user_memory``)."""
    for user_id, memory in memory_by_user.items():
        await db.upsert_user_memory(UserMemory(memory=memory, user_id=user_id))


def memory_rows(db: SessionDb) -> list[tuple[str | None, str | None]]:
    """``(user_id, memory)`` de cada documento de ``agno_memories``, em ordem."""
    return sorted((doc.get("user_id"), doc.get("memory")) for doc in collection(db, db.memory_table_name).docs)


def session_docs(db: SessionDb) -> list[dict[str, Any]]:
    """Documentos de ``agno_sessions`` como gravados."""
    return list(collection(db, db.session_table_name).docs)
