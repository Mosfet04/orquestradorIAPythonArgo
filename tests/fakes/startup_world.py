"""Startup real do app (``AppFactory`` -> container -> ``AgnoRuntime`` -> AgentOS) sem Mongo, LLM nem rede.

Usado pelos testes de ciclo de vida com uvicorn real (F2-05): no mesmo processo (thread) e num
subprocesso (SIGTERM de verdade). Só o Mongo (cliente do app com as coleções de config e o cliente
nativo do db de sessões) e o modelo são falsos; o resto da pilha é o de produção, inclusive o
``SessionDb`` (F2-08). Cada ponto de fechamento e de abertura registra um evento (``Events``):
``mongo:close`` (cliente do app, F3-09b), ``db:created`` e ``db:close`` (db de sessões e o cliente
dele, F3-09; os dois ``close`` são corrotinas e só registram quando aguardados), ``db:start``,
``http:start``/``http:stop``/``db:stop`` (lifespans do agno), ``runtime:close``,
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
from agno.models.response import ModelResponse
from bson import ObjectId
from fastapi import FastAPI

from src.infrastructure import dependency_injection as di
from src.infrastructure.repositories import mongo_base
from src.infrastructure.runtime.agno import AgnoRuntime
from src.infrastructure.runtime.agno import runtime as agno_runtime_module
from src.infrastructure.runtime.agno.session_db import SessionDb
from src.infrastructure.web import app_factory
from tests.fakes.dns import public_dns
from tests.fakes.models import FakeChatModel
from tests.fakes.mongo import FakeAsyncMongoClient, FakeMongoClient, FakeMongoCollection

FAIL_STEPS = ("container", "entities", "mount", "start")
CLEAN_ENV = (
    "API_KEY_RUN", "API_KEY_ADMIN", "APP_HOST", "ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS",
    "PLUGIN_ALLOWLIST", "ALLOW_DYNAMIC_IMPORT", "DYNAMIC_PROVIDER_SPECS", "MODEL_BASE_URL_ALLOWLIST", "SECRETS_DIR",
    "TOOL_ROUTE_ALLOWLIST",
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


class _AppMongoClient(FakeMongoClient):
    """Cliente Mongo do app (container, repositórios, diário e health; F3-09b): ping ok, registra o ``close``
    aguardado."""

    def __init__(self, events: Events, collections: dict[str, FakeMongoCollection]) -> None:
        super().__init__(collections)
        self._events = events

    async def close(self) -> None:
        await super().close()
        self._events.add("mongo:close")


class _SessionAsyncMongoClient(FakeAsyncMongoClient):
    """Cliente nativo do db de sessões (``AsyncMongo`` no nome: o agno reconhece o tipo por ele): registra
    o ``close`` aguardado e o uso depois dele."""

    def __init__(self, events: Events) -> None:
        super().__init__()
        self._events = events

    def refuse_if_closed(self) -> None:
        if self.closes:
            self._events.add("db:use-after-close")
        super().refuse_if_closed()

    async def close(self) -> None:
        await super().close()
        self._events.add("db:close")


class _WorldSessionDb(SessionDb):
    """O ``SessionDb`` de produção sobre o cliente falso; registra se o agno provisionar coleções."""

    def __init__(self, events: Events, db_url: str, db_name: str) -> None:
        super().__init__(_SessionAsyncMongoClient(events), db_url=db_url, db_name=db_name)  # type: ignore[arg-type]
        self._events = events

    async def _create_all_tables(self) -> None:
        self._events.add("db:provisioned")


def _db_lifespan_spy(events: Events, original: Any) -> Any:
    """``db_lifespan`` do agno que registra ``db:start``/``db:stop`` em volta do original."""

    @asynccontextmanager
    async def db_lifespan(app: FastAPI, agent_os: Any) -> AsyncIterator[None]:
        events.add("db:start")
        async with original(app, agent_os):
            yield
        events.add("db:stop")

    return db_lifespan


def _http_lifespan_spy(events: Events, original: Any, *, fail: bool) -> Any:
    """``http_client_lifespan`` do agno que registra início/fim; com ``fail``, quebra ao entrar."""

    @asynccontextmanager
    async def http_lifespan(app: FastAPI) -> AsyncIterator[None]:
        events.add("http:start")
        if fail:
            raise RuntimeError("falha injetada: start")
        async with original(app):
            yield
        events.add("http:stop")

    return http_lifespan


def _close_spy(events: Events, original: Any) -> Any:
    """``AgnoRuntime.close`` que registra ``runtime:close`` antes do original."""

    async def close(self: AgnoRuntime) -> None:
        events.add("runtime:close")
        await original(self)

    return close


def _warm_up_spy(original: Any, *, delay: float, fail: bool) -> Any:
    """Carga das entidades com espera opcional antes e, com ``fail``, falha no lugar da carga."""

    async def warm_up(self: Any) -> None:
        if delay:
            await asyncio.sleep(delay)
        if fail:
            raise RuntimeError("falha injetada: entities")
        await original(self)

    return warm_up


def _failing(step: str) -> Any:
    """Substituto que levanta ``RuntimeError("falha injetada: <step>")`` ao ser chamado."""

    def fail(*_: object, **__: object) -> None:
        raise RuntimeError(f"falha injetada: {step}")

    return fail


def assign_seed_ids() -> None:
    """Dá ``_id`` aos documentos semeados (o Mongo real sempre grava); chamar dentro do ``startup_world``."""
    database = mongo_base.MongoClientFactory.get_client("qualquer")["qualquer"]
    for name in ("agents_config", "teams_config"):
        for doc in database[name].docs:
            doc.setdefault("_id", ObjectId())


@contextmanager
def startup_world(
    events: Events,
    *,
    agents: tuple[str, ...] = ("a1",),
    teams: dict[str, list[str]] | None = None,
    fail_at: str | None = None,
    startup_delay: float = 0.0,
    run_delay: float = 0.0,
) -> Iterator[list[_AppMongoClient]]:
    """Troca Mongo, db de sessões e modelo por falsos e instala os espiões de ciclo de vida.

    ``fail_at``: etapa do startup que levanta ``RuntimeError("falha injetada: <etapa>")``
    (``container``: no wiring, depois de o cliente do app existir; ``entities``: na carga;
    ``mount``: ao montar as rotas do runtime; ``start``: num lifespan do agno).
    ``startup_delay``: espera (async) antes da carga das entidades. DNS falso (``public_dns``) na guarda da
    ``route`` de tool. Devolve o cliente do app quando alguém o pede à ``MongoClientFactory`` (antes, vazia).
    """
    if fail_at is not None and fail_at not in FAIL_STEPS:
        raise ValueError(f"etapa desconhecida: {fail_at}")
    clients: list[_AppMongoClient] = []
    collections: defaultdict[str, FakeMongoCollection] = defaultdict(_Collection)
    collections["agents_config"] = _Collection([agent_doc(a) for a in agents])
    collections["teams_config"] = _Collection([team_doc(t, m) for t, m in (teams or {}).items()])
    client = _AppMongoClient(events, collections)

    def get_client(cls: object, _conn: str) -> _AppMongoClient:
        if not clients:
            clients.append(client)
        return client

    def create_model(self: Any, config: Any) -> SlowChatModel:
        return SlowChatModel(id=config.model_id, responses=["resposta do modelo"] * 50, delay=run_delay, events=events)

    def new_session_db(db_url: str, db_name: str) -> _WorldSessionDb:
        events.add("db:created")
        return _WorldSessionDb(events, db_url, db_name)

    db_lifespan = _db_lifespan_spy(events, agno_os_app.db_lifespan)
    http_lifespan = _http_lifespan_spy(events, agno_os_app.http_client_lifespan, fail=fail_at == "start")
    close = _close_spy(events, AgnoRuntime.close)
    warm_up = _warm_up_spy(
        di.OrquestradorController.warm_up_cache, delay=startup_delay, fail=fail_at == "entities"
    )

    with ExitStack() as stack:
        enter = stack.enter_context
        enter(patch.object(mongo_base.MongoClientFactory, "get_client", classmethod(get_client)))
        enter(patch.object(app_factory, "setup_telemetry", lambda config: None))
        enter(patch.object(app_factory, "shutdown_telemetry", lambda: events.add("telemetry:shutdown")))
        enter(patch.object(di, "create_session_db", new_session_db))
        enter(patch.object(di.ProviderRegistry, "create_model", create_model))
        enter(patch.object(di, "system_resolver", public_dns))  # a guarda da route de tool não resolve host real
        enter(patch.object(agno_os_app, "db_lifespan", db_lifespan))
        enter(patch.object(agno_os_app, "http_client_lifespan", http_lifespan))
        enter(patch.object(AgnoRuntime, "close", close))
        enter(patch.object(di.OrquestradorController, "warm_up_cache", warm_up))
        if fail_at == "container":
            enter(patch.object(di, "MongoToolRepository", _failing("container")))
        if fail_at == "mount":
            enter(patch.object(agno_runtime_module, "build_agui_router", _failing("mount")))
        yield clients


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
