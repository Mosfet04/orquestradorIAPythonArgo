"""Startup real do app (``AppFactory`` -> container -> ``AgnoRuntime`` -> AgentOS) sem Mongo, LLM nem rede.

Usado pelos testes de ciclo de vida com uvicorn real (F2-05): no mesmo processo (thread) e num
subprocesso (SIGTERM de verdade). Só o Mongo (cliente motor e coleções de config), o db de sessões
das entidades e o modelo são falsos; o resto da pilha é o de produção. Cada ponto de fechamento e
de abertura registra um evento (``Events``): ``motor:close``, ``db:close``, ``db:start``,
``http:start``/``http:stop``/``db:stop`` (lifespans do agno 2.5.8), ``runtime:close``,
``telemetry:shutdown`` e ``model:start`` (run em andamento).

Como programa (``python -m tests.fakes.startup_world``): sobe o uvicorn num socket herdado
(``--fd``) e imprime cada evento como ``EVT <nome>`` no stdout.
"""

from __future__ import annotations

import argparse
import asyncio
import socket
import sys
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import ExitStack, asynccontextmanager, contextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import agno.os.app as agno_os_app
from agno.db.in_memory import InMemoryDb
from agno.models.response import ModelResponse
from fastapi import FastAPI

from src.infrastructure import dependency_injection as di
from src.infrastructure.repositories import mongo_base
from src.infrastructure.runtime.agno import AgnoRuntime, agent_factory_service, team_factory_service
from src.infrastructure.runtime.agno import runtime as agno_runtime_module
from src.infrastructure.web import app_factory
from tests.fakes.models import FakeChatModel
from tests.fakes.mongo import FakeMongoClient, FakeMongoCollection

FAIL_STEPS = ("container", "entities", "mount", "start")
CLEAN_ENV = (
    "API_KEY_RUN", "API_KEY_ADMIN", "APP_HOST", "ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS",
    "PLUGIN_ALLOWLIST", "ALLOW_DYNAMIC_IMPORT", "DYNAMIC_PROVIDER_SPECS", "MODEL_BASE_URL_ALLOWLIST", "SECRETS_DIR",
)


class _Collection(FakeMongoCollection):
    async def create_index(self, *_: object, **__: object) -> str:
        return "idx"


def agent_doc(agent_id: str) -> dict[str, Any]:
    return {
        "id": agent_id, "nome": agent_id, "factoryIaModel": "ollama", "model": f"m-{agent_id}",
        "descricao": "d", "prompt": "p", "tools_ids": [], "active": True,
    }


def team_doc(team_id: str, members: list[str]) -> dict[str, Any]:
    return {
        "id": team_id, "nome": team_id, "factoryIaModel": "ollama", "model": f"mt-{team_id}",
        "prompt": "p", "member_ids": members, "mode": "route", "active": True,
    }


@dataclass
class Events:
    """Eventos na ordem em que aconteceram; ``emit`` repassa cada um (ex.: stdout do subprocesso)."""

    names: list[str] = field(default_factory=list)
    emit: Callable[[str], None] | None = None

    def add(self, name: str) -> None:
        self.names.append(name)
        if self.emit is not None:
            self.emit(name)

    def count(self, name: str) -> int:
        return self.names.count(name)


@dataclass
class SlowChatModel(FakeChatModel):
    """Modelo roteirizado que demora ``delay`` s antes de responder (run em andamento)."""

    delay: float = 0.0
    events: Events | None = None

    async def _slow(self) -> None:
        if self.events is not None:
            self.events.add("model:start")
        await asyncio.sleep(self.delay)

    async def ainvoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        await self._slow()
        return self._next_response(*args, **kwargs)

    async def ainvoke_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ModelResponse]:
        await self._slow()
        yield self._next_response(*args, **kwargs)


class _Motor:
    """Cliente motor falso do container: ping ok, registra cada ``close``."""

    def __init__(self, events: Events) -> None:
        self._events = events
        self.admin = self
        self.closes = 0

    async def command(self, *_: object) -> dict[str, int]:
        return {"ok": 1}

    def close(self) -> None:
        self.closes += 1
        self._events.add("motor:close")


class _SessionDb(InMemoryDb):
    """Db de sessões em memória; como o ``MongoClient`` do pymongo, depois de ``close`` recusa o uso."""

    def __init__(self, events: Events) -> None:
        super().__init__()
        self._events = events
        self._closed = False

    def _refuse_if_closed(self) -> None:
        if self._closed:
            self._events.add("db:use-after-close")
            raise RuntimeError("Cannot use MongoClient after close")

    def upsert_session(self, *args: Any, **kwargs: Any) -> Any:
        self._refuse_if_closed()
        return super().upsert_session(*args, **kwargs)

    def get_session(self, *args: Any, **kwargs: Any) -> Any:
        self._refuse_if_closed()
        return super().get_session(*args, **kwargs)

    def _create_all_tables(self) -> None:
        self._events.add("db:provisioned")

    def close(self) -> None:
        self._closed = True
        self._events.add("db:close")


@contextmanager
def startup_world(
    events: Events,
    *,
    agents: tuple[str, ...] = ("a1",),
    teams: dict[str, list[str]] | None = None,
    fail_at: str | None = None,
    startup_delay: float = 0.0,
    run_delay: float = 0.0,
) -> Iterator[list[_Motor]]:
    """Troca Mongo, db de sessões e modelo por falsos e instala os espiões de ciclo de vida.

    ``fail_at``: etapa do startup que levanta ``RuntimeError("falha injetada: <etapa>")``
    (``container``: no wiring, depois de o cliente motor existir; ``entities``: na carga;
    ``mount``: ao montar as rotas do runtime; ``start``: num lifespan do agno).
    ``startup_delay``: espera (async) antes da carga das entidades. Devolve os clientes motor criados.
    """
    if fail_at is not None and fail_at not in FAIL_STEPS:
        raise ValueError(f"etapa desconhecida: {fail_at}")
    motors: list[_Motor] = []
    collections: defaultdict[str, FakeMongoCollection] = defaultdict(_Collection)
    collections["agents_config"] = _Collection([agent_doc(a) for a in agents])
    collections["teams_config"] = _Collection([team_doc(t, m) for t, m in (teams or {}).items()])
    client = FakeMongoClient(collections)

    def new_motor(*_: object, **__: object) -> _Motor:
        motor = _Motor(events)
        motors.append(motor)
        return motor

    def create_model(self: Any, config: Any) -> SlowChatModel:
        return SlowChatModel(id=config.model_id, responses=["resposta do modelo"] * 50, delay=run_delay, events=events)

    original_db = agno_os_app.db_lifespan
    original_http = agno_os_app.http_client_lifespan
    original_close = AgnoRuntime.close
    original_warm_up = di.OrquestradorController.warm_up_cache

    @asynccontextmanager
    async def db_lifespan(app: FastAPI, agent_os: Any) -> AsyncIterator[None]:
        events.add("db:start")
        async with original_db(app, agent_os):
            yield
        events.add("db:stop")

    @asynccontextmanager
    async def http_lifespan(app: FastAPI) -> AsyncIterator[None]:
        events.add("http:start")
        if fail_at == "start":
            raise RuntimeError("falha injetada: start")
        async with original_http(app):
            yield
        events.add("http:stop")

    async def close(self: AgnoRuntime) -> None:
        events.add("runtime:close")
        await original_close(self)

    async def warm_up(self: Any) -> None:
        if startup_delay:
            await asyncio.sleep(startup_delay)
        if fail_at == "entities":
            raise RuntimeError("falha injetada: entities")
        await original_warm_up(self)

    def broken_tool_repo(**_: object) -> None:
        raise RuntimeError("falha injetada: container")

    def broken_agui(*_: object, **__: object) -> None:
        raise RuntimeError("falha injetada: mount")

    def new_db(**_: object) -> _SessionDb:
        return _SessionDb(events)

    with ExitStack() as stack:
        enter = stack.enter_context
        enter(patch.object(mongo_base.MongoClientFactory, "get_client", classmethod(lambda cls, _conn: client)))
        enter(patch.object(di, "AsyncIOMotorClient", new_motor))
        enter(patch.object(app_factory, "setup_telemetry", lambda config: None))
        enter(patch.object(app_factory, "shutdown_telemetry", lambda: events.add("telemetry:shutdown")))
        enter(patch.object(agent_factory_service, "MongoAgentDb", new_db))
        enter(patch.object(team_factory_service, "MongoAgentDb", new_db))
        enter(patch.object(di.ProviderRegistry, "create_model", create_model))
        enter(patch.object(agno_os_app, "db_lifespan", db_lifespan))
        enter(patch.object(agno_os_app, "http_client_lifespan", http_lifespan))
        enter(patch.object(AgnoRuntime, "close", close))
        enter(patch.object(di.OrquestradorController, "warm_up_cache", warm_up))
        if fail_at == "container":
            enter(patch.object(di, "MongoToolRepository", broken_tool_repo))
        if fail_at == "mount":
            enter(patch.object(agno_runtime_module, "build_agui_router", broken_agui))
        yield motors


def clean_env() -> dict[str, str]:
    """Variáveis do ambiente atual sem as que mudam a config do app (chaves, host, CORS...)."""
    import os

    env = {k: v for k, v in os.environ.items() if k not in CLEAN_ENV}
    env.update(AGNO_TELEMETRY="false", OTEL_ENABLED="false", PYTHONUNBUFFERED="1")
    return env


# ── subprocesso ──────────────────────────────────────────────────────


def _emit(name: str) -> None:
    sys.stdout.write(f"EVT {name}\n")
    sys.stdout.flush()


def _main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--fd", type=int, required=True)
    parser.add_argument("--fail-at", default=None)
    parser.add_argument("--startup-delay", type=float, default=0.0)
    parser.add_argument("--run-delay", type=float, default=0.0)
    parser.add_argument("--graceful-timeout", type=int, default=None)
    parser.add_argument("--agents", default="a1")
    args = parser.parse_args()

    import uvicorn

    events = Events(emit=_emit)
    agents = tuple(args.agents.split(","))
    with startup_world(
        events,
        agents=agents,
        teams={"t1": list(agents)},
        fail_at=args.fail_at,
        startup_delay=args.startup_delay,
        run_delay=args.run_delay,
    ):
        app = app_factory.AppFactory().create_app()
        listener = socket.socket(fileno=args.fd)
        config = uvicorn.Config(
            app, log_level="info", lifespan="on", ws="websockets-sansio",
            timeout_graceful_shutdown=args.graceful_timeout,
        )
        server = uvicorn.Server(config)
        server.run(sockets=[listener])


if __name__ == "__main__":
    _main()
    sys.stdout.flush()
