"""Telemetria do Agno desligada (F1-03) em Agent, Team e AgentOS por padrão.

Agent/Team: ``tests/golden/test_agno_kwargs_golden.py`` prova ``telemetry=False`` em toda
montagem. Aqui: o AgentOS e o default de ``AGNO_TELEMETRY`` no startup.

F3-04: controle positivo neutro à versão. O 3.x manda a telemetria numa thread (fila) e o launch do
AgentOS num lifespan; um "nada foi enviado" sem drenar o envio passaria por vazio. O teste roda o app
de ponta a ponta (lifespan, run REST de agente e de team, AG-UI) e depois um Agent e um AgentOS de
controle com telemetria ligada: quando o controle chega ao gravador, o que o app mandou antes também
chegou (``tests/fakes/agno_telemetry.py``).

F3-08: os evals do AgentOS (``agno/os/routers/evals/utils.py``, agno 3.1.12) usam ``telemetry=True`` e
ignoram ``AGNO_TELEMETRY`` (``agno/eval/utils.py``, ``log_eval_telemetry``). Até a F8 as rotas
``/eval-runs*`` respondem 404 (``eval_runs_router``): nenhum eval roda e nada sai. O teste bloqueia a
rede (socket) e grava a telemetria do agno no ``httpx``; a drenagem é um evento de eval de controle pela
mesma função do agno.
"""

from __future__ import annotations

import os
from typing import Any, ClassVar

import agno.api.os as agno_api_os
import httpx
import pytest
from agno.agent import Agent
from agno.db.schemas.evals import EvalType
from agno.eval.utils import log_eval_telemetry
from agno.os import AgentOS

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.team_config import TeamConfig
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService
from src.infrastructure.runtime.agno.eval_runs_router import EVAL_RUNS_ROUTES
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from src.infrastructure.runtime.agno.team_factory_service import TeamFactoryService
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import FakeChatModel, FakeEmbedderFactory, FakeModelFactory, InMemoryToolRepository, RecordingLogger
from tests.fakes.agno_telemetry import AgnoTelemetryRecorder
from tests.fakes.network import block_network
from tests.fakes.session_db import fake_session_db
from tests.fakes.web import LOOPBACK_BASE_URL, LOOPBACK_CLIENT, mount_agent_os


class _RecordingAgentOS(AgentOS):
    created: ClassVar[list[AgentOS]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        _RecordingAgentOS.created.append(self)


def test_agent_os_montado_sem_telemetria(monkeypatch: pytest.MonkeyPatch):
    launches: list[object] = []

    def record_launch(launch: object) -> None:
        # O agno chama com keyword (``log_os_telemetry(launch=...)``): ``launches.append`` não serve.
        launches.append(launch)

    monkeypatch.setattr(agno_api_os, "log_os_telemetry", record_launch)
    monkeypatch.setattr("src.infrastructure.runtime.agno.runtime.AgentOS", _RecordingAgentOS)
    monkeypatch.setattr(_RecordingAgentOS, "created", [])
    agent = Agent(id="agente-1", name="Agente 1", model=FakeChatModel(responses=[]), telemetry=False)
    factory = AppFactory()

    mount_agent_os(factory, factory.create_app(), [agent], [])

    assert [os_.telemetry for os_ in _RecordingAgentOS.created] == [False]
    assert launches == [], "AgentOS registrou o launch na API da Agno"


def _reset_agno_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    # setenv antes do delenv: o monkeypatch passa a restaurar o valor original no teardown.
    monkeypatch.setenv("AGNO_TELEMETRY", "placeholder")
    monkeypatch.delenv("AGNO_TELEMETRY")


def test_startup_define_agno_telemetry_false_quando_ausente(monkeypatch: pytest.MonkeyPatch):
    _reset_agno_telemetry(monkeypatch)

    AppFactory().create_app()

    assert os.environ["AGNO_TELEMETRY"] == "false"


def test_startup_nao_sobrescreve_valor_explicito(monkeypatch: pytest.MonkeyPatch):
    _reset_agno_telemetry(monkeypatch)
    monkeypatch.setenv("AGNO_TELEMETRY", "true")

    AppFactory().create_app()

    assert os.environ["AGNO_TELEMETRY"] == "true"


async def _production_entities() -> tuple[Agent, Any]:
    """Agente e team pelas fábricas de produção (as que fixam ``telemetry=False``)."""
    logger = RecordingLogger()
    models = FakeModelFactory(responses=["ok"] * 10)
    agents = AgentFactoryService(
        db_url="mongodb://mongo.test.invalid:27017",
        session_db=None,
        logger=logger,
        model_factory=models,
        embedder_factory=FakeEmbedderFactory(),
        tool_factory=HttpToolFactory(logger=logger),
        tool_repository=InMemoryToolRepository(),
    )
    agent = await agents.create_agent(
        AgentConfig(id="ag", nome="ag", factory_ia_model="fake", model="m", descricao="d", prompt="p")
    )
    member = await agents.create_agent(
        AgentConfig(id="membro", nome="membro", factory_ia_model="fake", model="m", descricao="d", prompt="p")
    )
    team = TeamFactoryService(session_db=None, logger=logger, model_factory=models).create_team(
        TeamConfig(id="time", nome="time", factory_ia_model="fake", model="m", member_ids=["membro"]), [member]
    )
    return agent, team


def _control_agent(control_id: str) -> Agent:
    return Agent(id=control_id, name=control_id, model=FakeChatModel(responses=["ok"]), telemetry=True)


async def _barrier(recorder: AgnoTelemetryRecorder, control: Agent) -> None:
    """Run do agent de controle (telemetria ligada): quando o evento dele chega, a fila anterior já drenou."""
    await control.arun("oi")
    await recorder.wait_for(("/telemetry/runs", control.id))


async def test_controle_positivo_drena_o_envio_e_o_app_nao_manda_telemetria(monkeypatch: pytest.MonkeyPatch):
    recorder = AgnoTelemetryRecorder().install(monkeypatch)
    # Barreira no início: no 3.x a fila é um singleton do processo e o evento de outro teste do mesmo
    # worker (ex.: ``telemetry=True`` em ``test_qa_f1_03_edge``) pode chegar ao gravador depois.
    _reset_agno_telemetry(monkeypatch)  # sem AGNO_TELEMETRY=false, que desligaria o controle
    await _barrier(recorder, _control_agent("controle-inicial"))
    recorder.requests.clear()
    factory = AppFactory()
    app = factory.create_app()
    # Sem AGNO_TELEMETRY: quem desliga é o ``telemetry=False`` explícito das fábricas e do AgentOS.
    monkeypatch.delenv("AGNO_TELEMETRY")
    agent, team = await _production_entities()
    runtime = mount_agent_os(factory, app, [agent], [team])
    agui_body = {
        "threadId": "t1",
        "runId": "r1",
        "state": {},
        "messages": [{"id": "m1", "role": "user", "content": "oi"}],
        "tools": [],
        "context": [],
        "forwardedProps": {"user_id": "ana"},
    }

    await runtime.start()  # no 3.x, o launch do AgentOS sai num lifespan
    try:
        transport = httpx.ASGITransport(app=app, client=LOOPBACK_CLIENT)
        async with httpx.AsyncClient(transport=transport, base_url=LOOPBACK_BASE_URL) as client:
            responses = [
                await client.post("/agents/ag/runs", data={"message": "oi", "stream": "false", "user_id": "ana"}),
                await client.post("/teams/time/runs", data={"message": "oi", "stream": "false", "user_id": "ana"}),
                await client.post("/agui/ag", json=agui_body),
            ]
    finally:
        await runtime.close()
    assert [r.status_code for r in responses] == [200, 200, 200], [r.text[:200] for r in responses]
    assert [r.json()["status"] for r in responses[:2]] == ["COMPLETED", "COMPLETED"]
    assert "RUN_FINISHED" in responses[2].text

    control_agent = _control_agent("controle")
    control_os = AgentOS(id="os-controle", agents=[control_agent], telemetry=True)
    control_app = control_os.get_app()
    async with control_app.router.lifespan_context(control_app):
        pass
    await _barrier(recorder, control_agent)

    assert recorder.events() == [("/telemetry/os", "os-controle"), ("/telemetry/runs", "controle")]


async def _eval_barrier(recorder: AgnoTelemetryRecorder, run_id: str) -> None:
    """Evento de eval de controle pela função que os evals do agno usam (ignora ``AGNO_TELEMETRY``)."""
    log_eval_telemetry(run_id=run_id, eval_type=EvalType.PERFORMANCE)
    await recorder.wait_for(("/telemetry/evals", run_id))


EVAL_RUNS_REQUESTS = [(method, path.replace("{eval_run_id}", "qualquer")) for method, path in EVAL_RUNS_ROUTES]


async def test_eval_runs_responde_404_sem_egress_para_a_telemetria(monkeypatch: pytest.MonkeyPatch):
    network = block_network(monkeypatch)
    recorder = AgnoTelemetryRecorder().install(monkeypatch)
    await _eval_barrier(recorder, "controle-eval-inicial")  # evento atrasado de outro teste do worker
    recorder.requests.clear()
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    factory = AppFactory()
    app = factory.create_app()
    model = FakeChatModel(responses=["ok"] * 5)
    agent = Agent(id="ag", name="ag", model=model, telemetry=False, db=fake_session_db())
    runtime = mount_agent_os(factory, app, [agent], [])
    # Corpo válido para o agno: sem o 404, roda um eval de performance de verdade (1 run, sem warm-up).
    eval_body = {"agent_id": "ag", "eval_type": "performance", "input": "oi", "num_iterations": 1}

    await runtime.start()
    try:
        transport = httpx.ASGITransport(app=app, client=LOOPBACK_CLIENT)
        async with httpx.AsyncClient(transport=transport, base_url=LOOPBACK_BASE_URL) as client:
            responses = [
                await client.request(method, path, json=eval_body if method in ("POST", "PATCH") else None)
                for method, path in EVAL_RUNS_REQUESTS
            ]
    finally:
        await runtime.close()
    await _eval_barrier(recorder, "controle-eval")

    assert recorder.events() == [("/telemetry/evals", "controle-eval")]
    assert network.attempts == [], f"tentativas de rede: {network.attempts}"
    assert model.calls == [], "o eval rodou o agente"
    assert [r.status_code for r in responses] == [404] * len(EVAL_RUNS_REQUESTS), [r.text[:200] for r in responses]
