"""F2-05: ciclo de vida do AgentOS no startup real (``AppFactory`` -> container -> ``AgnoRuntime``).

Pilha real com só o Mongo (cliente motor e coleções de config) e o modelo falsos. Critérios:
- os lifespans do AgentOS (``db_lifespan`` e ``http_client_lifespan`` do agno 2.5.8) rodam no
  startup e no shutdown do app (B15: com ``get_app()`` dentro do nosso lifespan, não rodavam);
- o db de sessões de cada entidade é fechado uma vez no shutdown e o startup não provisiona
  coleções (I/O síncrono no event loop);
- shutdown fecha o runtime e o cliente Mongo do container uma vez, inclusive com falha parcial
  de startup.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, ClassVar

import agno.os.app as agno_os_app
import pytest
from agno.db.in_memory import InMemoryDb
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.infrastructure import dependency_injection as di
from src.infrastructure.repositories import mongo_base
from src.infrastructure.runtime.agno import agent_factory_service, team_factory_service
from src.infrastructure.web import app_factory
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import FakeChatModel, FakeMongoClient, FakeMongoCollection, RecordingLogger

pytestmark = pytest.mark.usefixtures("offline_knowledge")

LOCAL = {"base_url": "http://127.0.0.1:7777", "client": ("127.0.0.1", 50000)}
_ENV_CLEAR = (
    "API_KEY_RUN", "API_KEY_ADMIN", "APP_HOST", "ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS",
    "PLUGIN_ALLOWLIST", "ALLOW_DYNAMIC_IMPORT", "DYNAMIC_PROVIDER_SPECS", "MODEL_BASE_URL_ALLOWLIST", "SECRETS_DIR",
)


class _Motor:
    """Cliente motor falso do container: ping ok, conta os ``close``."""

    instances: ClassVar[list[_Motor]] = []

    def __init__(self, *_: object, **__: object) -> None:
        self.admin = self
        self.closes = 0
        _Motor.instances.append(self)

    async def command(self, *_: object) -> dict[str, int]:
        return {"ok": 1}

    def close(self) -> None:
        self.closes += 1


class _Collection(FakeMongoCollection):
    async def create_index(self, *_: object, **__: object) -> str:
        return "idx"


class SpyDb(InMemoryDb):
    """Db de sessões de uma entidade: conta provisionamento e fechamento."""

    def __init__(self) -> None:
        super().__init__()
        self.provisioned = 0
        self.closes = 0

    def _create_all_tables(self) -> None:
        self.provisioned += 1

    def close(self) -> None:
        self.closes += 1


@dataclass
class Spy:
    events: list[str] = field(default_factory=list)
    dbs: list[SpyDb] = field(default_factory=list)
    logger: RecordingLogger = field(default_factory=RecordingLogger)


def _agent(agent_id: str) -> dict[str, Any]:
    return {
        "id": agent_id, "nome": agent_id, "factoryIaModel": "ollama", "model": f"m-{agent_id}",
        "descricao": "d", "prompt": "p", "tools_ids": [], "active": True,
    }


def _team(team_id: str, members: list[str]) -> dict[str, Any]:
    return {
        "id": team_id, "nome": team_id, "factoryIaModel": "ollama", "model": f"mt-{team_id}",
        "prompt": "p", "member_ids": members, "mode": "route", "active": True,
    }


@pytest.fixture
def spy(monkeypatch: pytest.MonkeyPatch) -> Spy:
    """Startup real com Mongo e modelo falsos; espiões nos lifespans do AgentOS (agno/os/app.py)."""
    result = Spy()
    for name in _ENV_CLEAR:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    monkeypatch.setenv("OTEL_ENABLED", "false")
    _Motor.instances.clear()

    collections: defaultdict[str, FakeMongoCollection] = defaultdict(_Collection)
    collections["agents_config"] = _Collection([_agent("a1"), _agent("a2")])
    collections["teams_config"] = _Collection([_team("t1", ["a1", "a2"])])
    client = FakeMongoClient(collections)
    monkeypatch.setattr(mongo_base.MongoClientFactory, "get_client", classmethod(lambda cls, _conn: client))
    monkeypatch.setattr(di, "AsyncIOMotorClient", _Motor)
    monkeypatch.setattr(di, "StructlogLoggerAdapter", lambda _name: result.logger)
    monkeypatch.setattr(app_factory, "setup_telemetry", lambda config: None)
    monkeypatch.setattr(app_factory, "shutdown_telemetry", lambda: None)

    def new_db(**_: object) -> SpyDb:
        db = SpyDb()
        result.dbs.append(db)
        return db

    monkeypatch.setattr(agent_factory_service, "MongoAgentDb", new_db)
    monkeypatch.setattr(team_factory_service, "MongoAgentDb", new_db)
    monkeypatch.setattr(
        di.ProviderRegistry,
        "create_model",
        lambda self, config: FakeChatModel(id=config.model_id, responses=["ok"] * 5),
    )

    original_db = agno_os_app.db_lifespan
    original_http = agno_os_app.http_client_lifespan

    @asynccontextmanager
    async def db_lifespan(app: FastAPI, agent_os: Any) -> AsyncIterator[None]:
        result.events.append("db:start")
        async with original_db(app, agent_os):
            yield
        result.events.append("db:stop")

    @asynccontextmanager
    async def http_client_lifespan(app: FastAPI) -> AsyncIterator[None]:
        result.events.append("http:start")
        async with original_http(app):
            yield
        result.events.append("http:stop")

    monkeypatch.setattr(agno_os_app, "db_lifespan", db_lifespan)
    monkeypatch.setattr(agno_os_app, "http_client_lifespan", http_client_lifespan)
    return result


@pytest.fixture
def app(spy: Spy) -> Iterator[FastAPI]:
    yield AppFactory().create_app()


def test_lifespans_do_agentos_rodam_no_startup_e_no_shutdown_do_app(spy: Spy, app: FastAPI) -> None:
    own_lifespan = app.router.lifespan_context

    with TestClient(app, **LOCAL) as http:  # type: ignore[arg-type]
        assert http.get("/agents").status_code == 200
        during = list(spy.events)

    assert during == ["db:start", "http:start"]
    assert spy.events == ["db:start", "http:start", "http:stop", "db:stop"]
    # O lifespan do app segue o nosso: o get_app() não o troca pelo combinado (que o rodaria de novo).
    assert app.router.lifespan_context is own_lifespan


def test_shutdown_fecha_o_db_de_cada_entidade_uma_vez_e_o_startup_nao_provisiona(spy: Spy, app: FastAPI) -> None:
    with TestClient(app, **LOCAL) as http:  # type: ignore[arg-type]
        assert sorted(a["id"] for a in http.get("/agents").json()) == ["a1", "a2"]
        assert [t["id"] for t in http.get("/teams").json()] == ["t1"]
        assert [db.closes for db in spy.dbs] == [0, 0, 0]

    assert [(db.provisioned, db.closes) for db in spy.dbs] == [(0, 1), (0, 1), (0, 1)]
    assert [m.closes for m in _Motor.instances] == [1]


def test_falha_ao_carregar_entidades_fecha_o_cliente_mongo_uma_vez(
    spy: Spy, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    async def broken(self: Any) -> None:
        raise RuntimeError("carga quebrada")

    monkeypatch.setattr(di.OrquestradorController, "warm_up_cache", broken)

    with pytest.raises(RuntimeError, match="carga quebrada"):
        with TestClient(app, **LOCAL):  # type: ignore[arg-type]
            pass  # pragma: no cover - o startup não completa

    assert spy.events == []  # nada montado, nada a fechar no runtime
    assert [m.closes for m in _Motor.instances] == [1]


def test_falha_num_lifespan_do_agentos_recusa_o_startup_e_fecha_o_cliente_mongo_uma_vez(
    spy: Spy, app: FastAPI, monkeypatch: pytest.MonkeyPatch
) -> None:
    @asynccontextmanager
    async def failing(app: FastAPI) -> AsyncIterator[None]:
        spy.events.append("http:start")
        raise RuntimeError("lifespan do agno quebrado")
        yield  # pragma: no cover

    monkeypatch.setattr(agno_os_app, "http_client_lifespan", failing)

    with pytest.raises(RuntimeError, match="lifespan do agno quebrado"):
        with TestClient(app, **LOCAL):  # type: ignore[arg-type]
            pass  # pragma: no cover - o startup não completa

    # O startup falha como no AgentOS; o lifespan combinado do agno desfaz os já abertos (o
    # db_lifespan do agno 2.5.8 não tem try/finally: o fechamento dos dbs fica para o fim do
    # processo, que sai com o startup recusado). O cliente Mongo do container fecha uma vez.
    assert spy.events == ["db:start", "http:start"]
    assert [m.closes for m in _Motor.instances] == [1]
