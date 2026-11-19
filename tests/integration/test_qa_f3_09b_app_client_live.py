"""QA do F3-09b, ``live``: o composition root real sobre um Mongo de verdade (``MONGO_LIVE_URL``).

O cliente do app é o ``AsyncMongoClient`` da ``MongoClientFactory``: ping e health no mesmo cliente dos
repositórios, ``close`` aguardado uma vez no ``cleanup`` e, num container novo (outro cliente), tudo volta a
funcionar. A URL só vem do ambiente e não entra em ``assert``, id de teste nem log.
"""

from __future__ import annotations

import pytest
from pymongo import AsyncMongoClient
from pymongo.errors import InvalidOperation

from src.infrastructure import dependency_injection as di
from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.repositories.mongo_base import MongoClientFactory
from src.infrastructure.repositories.mongo_tool_repository import MongoToolRepository
from tests.fakes import RecordingLogger
from tests.fakes.live_mongo import LiveMongo
from tests.fakes.startup_world import CLEAN_ENV

pytestmark = pytest.mark.live


@pytest.fixture
def live_app_env(live_mongo: LiveMongo, monkeypatch: pytest.MonkeyPatch) -> LiveMongo:
    for name in CLEAN_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("MONGO_CONNECTION_STRING", live_mongo.url)
    monkeypatch.setenv("MONGO_DATABASE_NAME", live_mongo.database)
    monkeypatch.setenv("OTEL_ENABLED", "false")
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    monkeypatch.setattr(MongoClientFactory, "_instances", {})
    return live_mongo


async def test_container_real_usa_um_cliente_nativo_saudavel_e_o_cleanup_o_fecha_uma_vez(
    live_app_env: LiveMongo,
) -> None:
    container = await di.DependencyContainer.create_async(AppConfig.load())
    client = container._mongo_client
    try:
        assert type(client) is AsyncMongoClient
        health = await container.health_service.check_async()  # type: ignore[union-attr]
        repository = MongoToolRepository(connection_string=live_app_env.url, logger=RecordingLogger())
        assert repository._client is client
        assert await repository.ping() is True
        assert await client[live_app_env.database]["document_tree"].index_information() != {}  # índices do startup
    finally:
        await container.cleanup()
    await container.cleanup()  # idempotente

    assert health["checks"]["mongodb"] == {"status": "healthy"}
    assert MongoClientFactory._instances == {}
    with pytest.raises(InvalidOperation):
        await client.admin.command("ping")


async def test_container_novo_depois_do_cleanup_ganha_outro_cliente_que_funciona(live_app_env: LiveMongo) -> None:
    first = await di.DependencyContainer.create_async(AppConfig.load())
    first_client = first._mongo_client
    await first.cleanup()

    second = await di.DependencyContainer.create_async(AppConfig.load())
    try:
        second_client = second._mongo_client
        health = await second.health_service.check_async()  # type: ignore[union-attr]
    finally:
        await second.cleanup()

    assert second_client is not first_client
    assert health["checks"]["mongodb"] == {"status": "healthy"}
