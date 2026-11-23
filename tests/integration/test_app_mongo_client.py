"""F3-09b: um cliente Mongo do app (``AsyncMongoClient`` nativo) para repositórios, diário e health.

Composition root de produção sem nenhum fake de Mongo: a ``MongoClientFactory`` cria o cliente real e o
db de sessões cria o dele (``create_session_db``, F3-09). O detector (``trap_sync_pymongo``) intercepta a
escolha de servidor do pymongo síncrono e a do cliente nativo, registra e falha como um Mongo fora do ar,
sem abrir socket. As rotas ``/admin/config`` usam o caso de uso que o container monta.

Vermelho antes: os repositórios e o diário iam pelo motor (pymongo síncrono nas threads do executor),
o container abria um segundo cliente motor só para o ping e o health, e o cliente da fábrica nunca era
fechado.
"""

from __future__ import annotations

import httpx
import pytest

from src.infrastructure import dependency_injection as di
from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.repositories import mongo_base
from src.infrastructure.web.app_factory import AppFactory
from src.presentation.routes.admin_config import router as admin_config_router
from tests.fakes.admin_config_app import ADMIN, admin_env, tool_body
from tests.fakes.dns import public_dns
from tests.fakes.mongo import SyncPymongoCalls, trap_sync_pymongo
from tests.fakes.web import LOOPBACK_BASE_URL, LOOPBACK_CLIENT

DB_URL = "mongodb://mongo.test.invalid:27017"


@pytest.fixture
def pymongo_calls(monkeypatch: pytest.MonkeyPatch) -> SyncPymongoCalls:
    admin_env(monkeypatch)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    monkeypatch.setenv("MONGO_CONNECTION_STRING", DB_URL)
    monkeypatch.setattr(mongo_base.MongoClientFactory, "_instances", {})
    monkeypatch.setattr(di, "system_resolver", public_dns)  # a guarda da route de tool não resolve host real
    return trap_sync_pymongo(monkeypatch)


async def test_rotas_de_config_fazem_o_io_pelo_cliente_nativo_e_o_shutdown_fecha_um_cliente_do_app(
    pymongo_calls: SyncPymongoCalls,
) -> None:
    container = await di.DependencyContainer.create_async(AppConfig.load())  # sobe sem Mongo (ping falha)
    startup = list(pymongo_calls.async_operations)
    app = AppFactory().create_app(routers=[admin_config_router])
    app.state.config_admin = container.get_config_admin()
    transport = httpx.ASGITransport(app=app, client=LOOPBACK_CLIENT, raise_app_exceptions=False)
    async with httpx.AsyncClient(transport=transport, base_url=LOOPBACK_BASE_URL) as http:
        listed = await http.get("/admin/config/agents", headers=ADMIN)
        created = await http.post("/admin/config/tools", json=tool_body(id="cep", name="cep"), headers=ADMIN)
        history = await http.get("/admin/config/tools/cep/history", headers=ADMIN)
    routes = pymongo_calls.async_operations[len(startup) :]
    await container.cleanup()
    await container.cleanup()  # idempotente: nada fecha duas vezes

    assert [listed.status_code, created.status_code, history.status_code] == [500, 500, 500]
    # Startup (ping e índices da árvore) e rotas pelo cliente nativo, no loop: a listagem, a criação (que lê
    # as tools antes de gravar e para no erro do driver) e o histórico (leitura do diário, ``config_audit``).
    assert startup == ["ping", "createIndexes"]
    assert routes == ["find", "find", "find"]
    assert (pymongo_calls.operations, pymongo_calls.closes) == ([], [])
    # Dois clientes no processo, cada um fechado uma vez com close aguardado: o do app (repositórios,
    # diário e health) e o do db de sessões (F3-09).
    assert pymongo_calls.async_closes == 2
    assert mongo_base.MongoClientFactory._instances == {}
