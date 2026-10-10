"""``AgnoRuntime``: os handles são os objetos do agno que o AgentOS monta (F2-04) e o ciclo de vida
do AgentOS no app (F2-05)."""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

import agno.os.app as agno_os_app
import pytest
from agno.agent import Agent
from agno.os import AgentOS
from agno.team import Team
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.team_config import TeamConfig
from src.infrastructure.runtime.agno import AgnoRuntime, agent_factory_service, team_factory_service
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from src.infrastructure.runtime.agno.team_factory_service import TeamFactoryService
from tests.fakes import (
    FakeChatModel,
    FakeEmbedderFactory,
    FakeModelFactory,
    InMemoryToolRepository,
    RecordingLogger,
)

CONN = "mongodb://mongo.test.invalid:27017"


def _agent(agent_id: str) -> AgentConfig:
    return AgentConfig(id=agent_id, nome=agent_id, factory_ia_model="openai", model="m", descricao="d", prompt="p")


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> AgnoRuntime:
    monkeypatch.setattr(agent_factory_service, "MongoAgentDb", lambda **_: None)
    monkeypatch.setattr(team_factory_service, "MongoAgentDb", lambda **_: None)
    logger = RecordingLogger()
    models = FakeModelFactory()
    return AgnoRuntime(
        agent_factory=AgentFactoryService(
            db_url=CONN,
            logger=logger,
            model_factory=models,
            embedder_factory=FakeEmbedderFactory(),
            tool_factory=HttpToolFactory(logger=logger),
            tool_repository=InMemoryToolRepository(),
        ),
        team_factory=TeamFactoryService(db_url=CONN, logger=logger, model_factory=models),
    )


async def test_handles_sao_agent_e_team_do_agno_e_o_team_recebe_os_agentes_membros(runtime: AgnoRuntime):
    first = await runtime.build_agent(_agent("a1"))
    second = await runtime.build_agent(_agent("a2"))

    team = runtime.build_team(
        TeamConfig(id="t1", nome="T1", factory_ia_model="openai", model="m", member_ids=["a2"], mode="route"),
        [first, second],
    )

    assert isinstance(first, Agent) and isinstance(team, Team)
    assert team.members == [second]


# ── ciclo de vida do AgentOS (F2-05) ────────────────────────────────


def _mount(runtime: AgnoRuntime, app: FastAPI | None = None) -> FastAPI:
    app = app or FastAPI()
    agent = Agent(id="a1", name="a1", model=FakeChatModel(responses=[]), telemetry=False)
    runtime.mount(app, [agent], [], cors_allowed_origins=["https://painel.example.com"])
    return app


def test_mount_monta_rotas_do_agentos_cancel_e_agui_sem_trocar_o_lifespan_do_app(runtime: AgnoRuntime):
    app = FastAPI()
    own = app.router.lifespan_context

    _mount(runtime, app)

    paths = {getattr(route, "path", None) for route in app.routes}
    assert {"/agents", "/agents/{agent_id}/runs/{run_id}/cancel", "/agui/{entity_id}", "/agui"} <= paths
    assert app.router.lifespan_context is own


@pytest.mark.parametrize("with_team", [True, False], ids=["com-team", "sem-teams"])
def test_mount_serve_os_agentes_e_os_teams_recebidos(runtime: AgnoRuntime, with_team: bool):
    """Com AgentOS real: o que foi montado é o que ``/agents`` e ``/teams`` listam (lista de teams vazia inclusive)."""
    app = FastAPI()
    agent = Agent(id="a1", name="a1", model=FakeChatModel(responses=[]), telemetry=False)
    team = Team(id="t1", name="t1", members=[agent], model=FakeChatModel(responses=[]), telemetry=False)

    runtime.mount(app, [agent], [team] if with_team else [], cors_allowed_origins=[])

    client = TestClient(app)
    assert [item["id"] for item in client.get("/agents").json()] == ["a1"]
    assert [item["id"] for item in client.get("/teams").json()] == (["t1"] if with_team else [])


def test_mount_de_novo_no_mesmo_runtime_e_recusado(runtime: AgnoRuntime):
    _mount(runtime)

    with pytest.raises(RuntimeError, match="já montado"):
        _mount(runtime)


def test_falha_no_get_app_devolve_o_lifespan_do_app_e_deixa_o_runtime_sem_montagem(
    runtime: AgnoRuntime, monkeypatch: pytest.MonkeyPatch
):
    def broken(self: AgentOS) -> FastAPI:
        raise RuntimeError("get_app quebrado")

    monkeypatch.setattr(AgentOS, "get_app", broken)
    app = FastAPI()
    own = app.router.lifespan_context

    with pytest.raises(RuntimeError, match="get_app quebrado"):
        _mount(runtime, app)

    assert app.router.lifespan_context is own
    monkeypatch.undo()
    _mount(runtime)  # a falha não marcou o runtime como montado


async def test_start_e_close_abrem_e_fecham_os_lifespans_do_agentos_uma_vez(
    runtime: AgnoRuntime, monkeypatch: pytest.MonkeyPatch
):
    events: list[str] = []

    @asynccontextmanager
    async def http_client_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        events.append("start")
        yield
        events.append("stop")

    monkeypatch.setattr(agno_os_app, "http_client_lifespan", http_client_lifespan)
    _mount(runtime)

    await runtime.start()
    await runtime.start()  # já aberto: não abre de novo
    assert events == ["start"]
    await runtime.close()
    await runtime.close()  # idempotente
    assert events == ["start", "stop"]


async def test_start_e_close_sem_mount_nao_fazem_nada(runtime: AgnoRuntime, monkeypatch: pytest.MonkeyPatch):
    events: list[str] = []

    @asynccontextmanager
    async def http_client_lifespan(_app: FastAPI) -> AsyncIterator[None]:
        events.append("start")  # pragma: no cover - não pode rodar sem mount
        yield  # pragma: no cover
        events.append("stop")  # pragma: no cover

    monkeypatch.setattr(agno_os_app, "http_client_lifespan", http_client_lifespan)

    await runtime.start()
    await runtime.close()

    assert events == []
