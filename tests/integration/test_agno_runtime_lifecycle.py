"""F2-05: ciclo de vida do AgentOS no startup real (``AppFactory`` -> container -> ``AgnoRuntime``).

Pilha real com só o Mongo (cliente motor e coleções de config) e o modelo falsos. Critérios:
- os lifespans do AgentOS (``db_lifespan`` e ``http_client_lifespan`` do agno 2.5.8) rodam no
  startup e no shutdown do app (B15: com ``get_app()`` dentro do nosso lifespan, não rodavam);
- F2-08: agentes, teams e AgentOS dividem um db de sessões só (o ``SessionDb``, assíncrono); o
  shutdown fecha o cliente dele uma vez e fora do event loop, e o startup não provisiona coleções;
- shutdown fecha o runtime, o db de sessões e o cliente Mongo do container uma vez (nenhum ``close``
  no event loop), inclusive com falha parcial de startup.
"""

from __future__ import annotations

from collections import defaultdict
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, ClassVar

import agno.os.app as agno_os_app
import pytest
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.infrastructure import dependency_injection as di
from src.infrastructure.repositories import mongo_base
from src.infrastructure.runtime.agno.session_db import SessionDb
from src.infrastructure.web import app_factory
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import FakeChatModel, FakeMongoClient, FakeMongoCollection, RecordingLogger, running_on_event_loop
from tests.fakes.session_db import session_db_client, session_docs, use_fake_session_db

pytestmark = pytest.mark.usefixtures("offline_knowledge")

LOCAL = {"base_url": "http://127.0.0.1:7777", "client": ("127.0.0.1", 50000)}
_ENV_CLEAR = (
    "API_KEY_RUN", "API_KEY_ADMIN", "APP_HOST", "ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS",
    "PLUGIN_ALLOWLIST", "ALLOW_DYNAMIC_IMPORT", "DYNAMIC_PROVIDER_SPECS", "MODEL_BASE_URL_ALLOWLIST", "SECRETS_DIR",
)


class _Motor:
    """Cliente motor falso do container: ping ok, conta os ``close`` (e os feitos no event loop)."""

    instances: ClassVar[list[_Motor]] = []

    def __init__(self, *_: object, **__: object) -> None:
        self.admin = self
        self.closes = 0
        self.closes_on_event_loop = 0
        _Motor.instances.append(self)

    async def command(self, *_: object) -> dict[str, int]:
        return {"ok": 1}

    def close(self) -> None:
        self.closes += 1
        self.closes_on_event_loop += running_on_event_loop()


class _Collection(FakeMongoCollection):
    async def create_index(self, *_: object, **__: object) -> str:
        return "idx"


@dataclass
class Spy:
    events: list[str] = field(default_factory=list)
    dbs: list[SessionDb] = field(default_factory=list)
    """Dbs de sessões criados pelo composition root (o ``SessionDb`` real sobre o cliente em memória)."""
    logger: RecordingLogger = field(default_factory=RecordingLogger)

    def session_client_closes(self) -> list[list[bool]]:
        """Por db de sessões: um item por ``close`` do cliente (``True`` = rodou no event loop)."""
        return [session_db_client(db).closes for db in self.dbs]


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

    result.dbs = use_fake_session_db(monkeypatch)
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


def test_agentes_teams_e_agentos_dividem_um_db_de_sessoes(spy: Spy, app: FastAPI) -> None:
    form = {"message": "oi", "stream": "false", "user_id": "ana"}
    with TestClient(app, **LOCAL) as http:  # type: ignore[arg-type]
        config = http.get("/config").json()
        agent_run = http.post("/agents/a1/runs", data={**form, "session_id": "s-agente"})
        team_run = http.post("/teams/t1/runs", data={**form, "session_id": "s-team"})
        listed = {
            kind: [item["session_id"] for item in http.get("/sessions", params={"type": kind}).json()["data"]]
            for kind in ("agent", "team")
        }

    assert (agent_run.status_code, team_run.status_code) == (200, 200)
    assert (agent_run.json()["status"], team_run.json()["status"]) == ("COMPLETED", "COMPLETED")
    # Dois agentes e um team: o composition root criou um db de sessões só, e é ele o do AgentOS.
    assert len(spy.dbs) == 1
    db = spy.dbs[0]
    assert (config["os_database"], config["databases"]) == (db.id, [db.id])
    # O agente e o team gravaram no mesmo banco, e as rotas do AgentOS leem dele.
    assert sorted((d["session_id"], d["session_type"], d["user_id"]) for d in session_docs(db)) == [
        ("s-agente", "agent", "ana"),
        ("s-team", "team", "ana"),
    ]
    assert listed == {"agent": ["s-agente"], "team": ["s-team"]}


def test_shutdown_fecha_o_cliente_do_db_de_sessoes_uma_vez_fora_do_event_loop_e_o_startup_nao_provisiona(
    spy: Spy, app: FastAPI
) -> None:
    with TestClient(app, **LOCAL) as http:  # type: ignore[arg-type]
        assert sorted(a["id"] for a in http.get("/agents").json()) == ["a1", "a2"]
        assert [t["id"] for t in http.get("/teams").json()] == ["t1"]
        assert spy.session_client_closes() == [[]]
        # Sem provisionamento: o startup não tocou em nenhuma coleção do db de sessões.
        assert session_db_client(spy.dbs[0]).databases == {}

    # Um cliente (não um por entidade), fechado uma vez e numa thread: o AgentOS e o container
    # pedem o close, e só o primeiro fecha. O cliente do container também fecha fora do loop.
    assert spy.session_client_closes() == [[False]]
    assert [(m.closes, m.closes_on_event_loop) for m in _Motor.instances] == [(1, 0)]


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
    assert [(m.closes, m.closes_on_event_loop) for m in _Motor.instances] == [(1, 0)]
    assert spy.session_client_closes() == [[False]]  # o container fecha o db que criou


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

    # O startup falha como no AgentOS; o db_lifespan do agno 2.5.8 não tem try/finally e não chega
    # a fechar o db, mas o container fecha o db de sessões que criou (F2-08) e o cliente Mongo dele,
    # uma vez cada e fora do event loop.
    assert spy.events == ["db:start", "http:start"]
    assert [(m.closes, m.closes_on_event_loop) for m in _Motor.instances] == [(1, 0)]
    assert spy.session_client_closes() == [[False]]
