"""QA do F3-09b: ciclo de vida do cliente Mongo do app (``AsyncMongoClient``, um só por processo e container).

O composition root é o de produção; só o ``AsyncMongoClient`` que a ``MongoClientFactory`` cria é trocado
pelo ``FakeAsyncMongoClient`` (que recusa uso depois do ``close`` e só conta o ``close`` aguardado). Critério
do item: um cliente do app, fechado uma vez no shutdown, com ``close`` aguardado, e um container novo (em
outro loop) nunca herda um cliente fechado.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from pymongo.errors import InvalidOperation

from src.infrastructure import dependency_injection as di
from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.repositories import mongo_base
from src.infrastructure.repositories.mongo_base import MongoClientFactory
from src.infrastructure.repositories.mongo_tool_repository import MongoToolRepository
from tests.fakes import RecordingLogger
from tests.fakes.mongo import FakeAsyncMongoClient, FakeMongoClient, FakeMongoCollection
from tests.fakes.startup_world import CLEAN_ENV

URL = "mongodb://mongo.test.invalid:27017"
MARKER = "marcador-de-segredo-do-driver"  # texto de exceção do driver que não pode ir ao log


@pytest.fixture
def created(monkeypatch: pytest.MonkeyPatch) -> list[FakeAsyncMongoClient]:
    """Cada ``AsyncMongoClient`` que a fábrica criou (cache zerado)."""
    for name in CLEAN_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MONGO_CONNECTION_STRING", URL)
    monkeypatch.setenv("MONGO_DATABASE_NAME", "qa_db")
    monkeypatch.setenv("OTEL_ENABLED", "false")
    clients: list[FakeAsyncMongoClient] = []

    def new_client(*_: object, **__: object) -> FakeAsyncMongoClient:
        clients.append(FakeAsyncMongoClient())
        return clients[-1]

    monkeypatch.setattr(MongoClientFactory, "_instances", {})
    monkeypatch.setattr(mongo_base, "AsyncMongoClient", new_client)
    return clients


async def test_dois_containers_em_sequencia_usam_clientes_diferentes_e_o_segundo_funciona(
    created: list[FakeAsyncMongoClient],
) -> None:
    first = await di.DependencyContainer.create_async(AppConfig.load())
    await first.cleanup()

    second = await di.DependencyContainer.create_async(AppConfig.load())
    try:
        health = await second.health_service.check_async()  # type: ignore[union-attr]
    finally:
        await second.cleanup()

    assert len(created) == 2
    assert [c.closes for c in created] == [[True], [True]]
    assert health["checks"]["mongodb"] == {"status": "healthy"}


async def test_cliente_fechado_pelo_cleanup_recusa_uso_e_nao_sobra_no_cache(
    created: list[FakeAsyncMongoClient],
) -> None:
    container = await di.DependencyContainer.create_async(AppConfig.load())
    [client] = created
    await container.cleanup()

    with pytest.raises(InvalidOperation):
        await client["qa_db"]["tools"].find_one({})
    assert MongoClientFactory._instances == {}


async def test_cleanups_concorrentes_fecham_o_cliente_do_app_uma_vez(created: list[FakeAsyncMongoClient]) -> None:
    container = await di.DependencyContainer.create_async(AppConfig.load())

    await asyncio.gather(container.cleanup(), container.cleanup(), container.cleanup())

    assert [c.closes for c in created] == [[True]]


async def test_close_do_cliente_que_falha_loga_so_o_tipo_e_o_cliente_ja_saiu_do_cache(
    created: list[FakeAsyncMongoClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    logger = RecordingLogger()
    monkeypatch.setattr(di, "StructlogLoggerAdapter", lambda _name: logger)
    container = await di.DependencyContainer.create_async(AppConfig.load())
    [client] = created

    async def broken_close() -> None:
        raise ConnectionError(f"falha ao falar com {MARKER}")

    client.close = broken_close  # type: ignore[method-assign]
    await container.cleanup()  # não propaga
    await container.cleanup()  # e não tenta de novo

    warnings = [(r.message, r.context) for r in logger.records if r.level == "warning"]
    assert warnings == [("Falha ao fechar o cliente MongoDB", {"error_type": "ConnectionError"})]
    assert MARKER not in repr(logger.records)
    assert MongoClientFactory._instances == {}


async def test_falha_ao_fechar_o_db_de_sessoes_nao_deixa_o_cliente_do_app_aberto(
    created: list[FakeAsyncMongoClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    container = await di.DependencyContainer.create_async(AppConfig.load())
    assert container._session_db is not None

    async def broken_close() -> None:
        raise RuntimeError("db de sessões não fecha")

    monkeypatch.setattr(container._session_db, "close", broken_close)
    await container.cleanup()

    # created[0] é o cliente do app (a fábrica); o do db de sessões não passa por ela.
    assert [c.closes for c in created] == [[True]]


async def test_o_cliente_do_app_e_o_mesmo_de_container_repositorios_e_health_e_nada_usa_outro(
    created: list[FakeAsyncMongoClient],
) -> None:
    container = await di.DependencyContainer.create_async(AppConfig.load())
    try:
        health = await container.health_service.check_async()  # type: ignore[union-attr]
        repository = MongoToolRepository(connection_string=URL, logger=RecordingLogger())
        assert container._mongo_client is created[0]
        assert repository._client is created[0]
    finally:
        await container.cleanup()

    assert health["checks"]["mongodb"] == {"status": "healthy"}
    assert len(created) == 1
    assert created[0].closes == [True]


async def test_container_sem_mongo_no_ping_ainda_fecha_o_cliente_no_cleanup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ping que falha não impede o app de subir nem o ``cleanup`` de fechar o cliente."""
    for name in CLEAN_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MONGO_CONNECTION_STRING", URL)
    monkeypatch.setenv("OTEL_ENABLED", "false")
    monkeypatch.setattr(MongoClientFactory, "_instances", {})
    client = FakeAsyncMongoClient()

    async def failing_ping(self: Any, *_: Any) -> None:
        raise ConnectionError("sem servidor")

    monkeypatch.setattr(type(client["admin"]), "command", failing_ping)
    monkeypatch.setattr(mongo_base, "AsyncMongoClient", lambda *_, **__: client)

    container = await di.DependencyContainer.create_async(AppConfig.load())
    health = await container.health_service.check_async()  # type: ignore[union-attr]
    await container.cleanup()

    assert health["checks"]["mongodb"] == {"status": "unhealthy"}
    assert client.closes == [True]


# ── o fake do cliente do repositório também recusa uso depois do close (R4 da revisão) ──


async def test_fake_mongo_client_recusa_toda_operacao_depois_do_close_e_aceita_antes() -> None:
    client = FakeMongoClient({"tools": FakeMongoCollection()})
    tools = client["qualquer_banco"]["tools"]
    await tools.insert_one({"id": "t1"})
    assert await tools.find_one({"id": "t1"}) is not None
    assert await client.admin.command("ping") == {"ok": 1}

    await client.close()

    for operation in (
        tools.find_one({"id": "t1"}),
        tools.insert_one({"id": "t2"}),
        tools.delete_one({"id": "t1"}),
        tools.count_documents({}),
        tools.create_index("id", name="idx"),
        client.admin.command("ping"),
    ):
        with pytest.raises(InvalidOperation):
            await operation
    with pytest.raises(InvalidOperation, match="Cannot use AsyncMongoClient after close"):
        tools.find({})
    assert client.closes == [True]
    assert [d["id"] for d in tools.docs] == ["t1"]  # o que foi recusado não gravou


async def test_fake_mongo_client_com_defaultdict_amarra_a_colecao_criada_depois_ao_cliente() -> None:
    from collections import defaultdict

    client = FakeMongoClient(defaultdict(FakeMongoCollection))
    await client["db"]["nova"].insert_one({"a": 1})
    await client.close()

    with pytest.raises(InvalidOperation):
        await client["db"]["outra_nova"].find_one({})
    with pytest.raises(InvalidOperation):
        await client["db"]["nova"].find_one({})
