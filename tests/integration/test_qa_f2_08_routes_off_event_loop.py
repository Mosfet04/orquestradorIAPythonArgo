"""QA do F2-08: nenhuma chamada do pymongo síncrono no event loop pelas rotas do app (REST, stream,
AG-UI, team, cancelamento) com o ``SessionDb`` de produção (motor real, sem servidor).

Complementa ``test_session_io_off_event_loop.py`` (que chama ``Agent.arun``/``Team.arun`` direto): aqui a
entrada é HTTP, pelo AgentOS montado como no lifespan do app. O detector (``trap_sync_pymongo``) falha
cada operação como um Mongo fora do ar, sem abrir socket; o run segue (o agno loga a falha de sessão).
O que se confere é onde o I/O de sessão rodou (nunca na thread do loop) e que ele aconteceu.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Iterator
from typing import Any

import httpx
import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.run.cancel import get_cancellation_manager
from agno.team import Team
from fastapi import FastAPI

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.team_config import TeamConfig
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from src.infrastructure.runtime.agno.session_db import SessionDb, create_session_db
from src.infrastructure.runtime.agno.team_factory_service import TeamFactoryService
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import (
    FakeChatModel,
    FakeEmbedderFactory,
    FakeModelFactory,
    InMemoryToolRepository,
    RecordingLogger,
)
from tests.fakes.mongo import SyncPymongoCalls, trap_sync_pymongo
from tests.fakes.web import mount_agent_os

DB_URL = "mongodb://mongo.test.invalid:27017"
RUN_KEY = "qa8-run-key-" + "r" * 22
ADMIN_KEY = "qa8-admin-key-" + "a" * 20
RUN = {"Authorization": f"Bearer {RUN_KEY}"}
_ENV_NAMES = ("ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS", "APP_HOST", "API_KEY_RUN", "API_KEY_ADMIN")
AGUI_BODY = {
    "threadId": "s-agui", "runId": "r1", "state": {}, "tools": [], "context": [], "forwardedProps": {},
    "messages": [{"id": "m1", "role": "user", "content": "oi"}],
}


class TickingModel(FakeChatModel):
    """Um trecho a cada 20 ms no stream, ou uma chamada de 0,5 s (o agno checa o cancelamento entre trechos)."""

    async def ainvoke_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ModelResponse]:
        for index in range(500):
            yield ModelResponse(role="assistant", content=f"t{index} ")
            await asyncio.sleep(0.02)

    async def ainvoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        await asyncio.sleep(0.5)
        return ModelResponse(role="assistant", content="fim")


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> SyncPymongoCalls:
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    return trap_sync_pymongo(monkeypatch)


async def _agent(db: SessionDb, agent_id: str, *, memory: bool = False, summary: bool = True) -> Agent:
    logger = RecordingLogger()
    service = AgentFactoryService(
        db_url=DB_URL,
        session_db=db,
        logger=logger,
        model_factory=FakeModelFactory(responses=["resposta"] * 20),
        embedder_factory=FakeEmbedderFactory(),
        tool_factory=HttpToolFactory(logger=logger),
        tool_repository=InMemoryToolRepository(),
    )
    return await service.create_agent(
        AgentConfig(
            id=agent_id, nome=agent_id, factory_ia_model="fake", model="m", descricao="d", prompt="p",
            user_memory_active=memory, summary_active=summary,
        )
    )


def _team(db: SessionDb, member: Agent) -> Team:
    service = TeamFactoryService(
        session_db=db, logger=RecordingLogger(), model_factory=FakeModelFactory(responses=["do time"] * 20)
    )
    return service.create_team(
        TeamConfig(id="time", nome="time", factory_ia_model="fake", model="mt", member_ids=[member.id or ""],
                   mode="coordinate", summary_active=True),
        [member],
    )


def _app(monkeypatch: pytest.MonkeyPatch, db: SessionDb, agents: list[Agent], teams: list[Team]) -> FastAPI:
    monkeypatch.setenv("APP_HOST", "0.0.0" + ".0")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("API_KEY_RUN", RUN_KEY)
    monkeypatch.setenv("API_KEY_ADMIN", ADMIN_KEY)
    factory = AppFactory()
    app = factory.create_app()
    mount_agent_os(factory, app, agents, teams, session_db=db)
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=RUN)


@pytest.fixture
async def db() -> AsyncIterator[SessionDb]:
    session_db = create_session_db(DB_URL, "agno")
    yield session_db
    await session_db.close()


@pytest.mark.parametrize("stream", ["false", "true"])
@pytest.mark.parametrize(("memory", "summary"), [(False, False), (False, True), (True, True)],
                         ids=["simples", "sumario", "memoria-e-sumario"])
async def test_run_rest_de_agente_nao_chama_o_pymongo_sincrono_no_event_loop(
    monkeypatch: pytest.MonkeyPatch, calls: SyncPymongoCalls, db: SessionDb, stream: str, memory: bool, summary: bool
) -> None:
    agent = await _agent(db, "ag", memory=memory, summary=summary)
    app = _app(monkeypatch, db, [agent], [])

    async with _client(app) as client:
        response = await client.post(
            "/agents/ag/runs", data={"message": "oi", "stream": stream, "user_id": "ana", "session_id": "s1"}
        )

    assert response.status_code == 200
    assert calls.operations, "o run não foi ao banco de sessões"
    assert calls.on_event_loop() == []


async def test_run_de_team_nao_chama_o_pymongo_sincrono_no_event_loop(
    monkeypatch: pytest.MonkeyPatch, calls: SyncPymongoCalls, db: SessionDb
) -> None:
    member = await _agent(db, "membro")
    app = _app(monkeypatch, db, [member], [_team(db, member)])

    async with _client(app) as client:
        response = await client.post(
            "/teams/time/runs", data={"message": "oi", "stream": "false", "user_id": "ana", "session_id": "s1"}
        )

    assert response.status_code == 200
    assert calls.operations, "o run do team não foi ao banco de sessões"
    assert calls.on_event_loop() == []


async def test_run_agui_nao_chama_o_pymongo_sincrono_no_event_loop(
    monkeypatch: pytest.MonkeyPatch, calls: SyncPymongoCalls, db: SessionDb
) -> None:
    agent = await _agent(db, "ag")
    app = _app(monkeypatch, db, [agent], [])

    async with _client(app) as client:
        response = await client.post("/agui/ag", json=AGUI_BODY)

    assert response.status_code == 200 and "RUN_FINISHED" in response.text
    assert calls.operations, "o run AG-UI não foi ao banco de sessões"
    assert calls.on_event_loop() == []


@pytest.mark.parametrize("stream", ["false", "true"])
async def test_cancelamento_de_run_nao_chama_o_pymongo_sincrono_no_event_loop(
    monkeypatch: pytest.MonkeyPatch, calls: SyncPymongoCalls, db: SessionDb, stream: str
) -> None:
    agent = Agent(id="tic", name="tic", model=TickingModel(), db=db, telemetry=False)
    app = _app(monkeypatch, db, [agent], [])

    async with _client(app) as client:
        run = asyncio.create_task(client.post(
            "/agents/tic/runs", data={"message": "oi", "stream": stream, "user_id": "ana", "session_id": "sc"}
        ))
        for _ in range(1000):
            if get_cancellation_manager().get_active_runs():
                break
            await asyncio.sleep(0.01)
        run_id = next(iter(get_cancellation_manager().get_active_runs()))
        cancel = await client.post(f"/agents/tic/runs/{run_id}/cancel")
        response = await asyncio.wait_for(run, timeout=15)

    assert cancel.status_code == 200 and response.status_code == 200
    assert get_cancellation_manager().get_active_runs() == {}
    assert calls.operations
    assert calls.on_event_loop() == []


@pytest.fixture(autouse=True)
def _no_leftover_runs() -> Iterator[None]:
    yield
    assert get_cancellation_manager().get_active_runs() == {}
