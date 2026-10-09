"""F1-10: cancelamento de run só para run registrado (em andamento).

O agno 2.5.8 grava intenção de cancelamento para qualquer ``run_id``, mesmo inexistente
(``agno/run/cancellation_management/in_memory_cancellation_manager.py``, ``cancel_run``), e
as rotas ``POST /agents|teams/{id}/runs/{run_id}/cancel`` sempre respondiam 200. Um cliente
enchia a memória com ids inventados e pré-cancelava o próximo run com um ``runId`` que ele
adivinha (o do AG-UI é escolhido pelo cliente). Pilha real: ``AppFactory.create_app`` (CORS +
auth) -> ``_mount_agent_os`` -> AgentOS + AG-UI; modelo = ``FakeChatModel``.
"""

from __future__ import annotations

import asyncio
import uuid
from typing import Any

import httpx
import pytest
from ag_ui.core import RunErrorEvent, RunFinishedEvent
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.run.cancel import get_cancellation_manager
from agno.team import Team
from fastapi import FastAPI
from fastapi.routing import APIRoute

from src.infrastructure.web.app_factory import AppFactory
from src.infrastructure.web.run_cancellation import RegisteredRunCancellationManager
from tests.fakes import FakeChatModel
from tests.fakes.agui import assert_valid_run, parse_agui_sse, text_of

RUN_KEY = "f110-run-key-" + "r" * 20
ADMIN_KEY = "f110-admin-key-" + "a" * 18
RUN = {"Authorization": f"Bearer {RUN_KEY}"}
_ENV_NAMES = ("ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS", "APP_HOST", "API_KEY_RUN", "API_KEY_ADMIN")


class _TickingModel(FakeChatModel):
    """Emite um trecho a cada 20 ms (até 500): o agno checa o cancelamento entre trechos."""

    async def ainvoke_stream(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def,override]
        for index in range(500):
            yield ModelResponse(role="assistant", content=f"t{index} ")
            await asyncio.sleep(0.02)


def _mount(monkeypatch: pytest.MonkeyPatch, agents: list[Agent], teams: list[Team]) -> FastAPI:
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    env = {
        "APP_HOST": "0.0.0" + ".0",
        "ENVIRONMENT": "production",
        "API_KEY_RUN": RUN_KEY,
        "API_KEY_ADMIN": ADMIN_KEY,
    }
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    factory = AppFactory()
    app = factory.create_app()
    factory._mount_agent_os(app, agents, teams)
    return app


def _agent(agent_id: str, model: FakeChatModel | None = None) -> Agent:
    return Agent(id=agent_id, name=agent_id, model=model or FakeChatModel(responses=["oi"]), telemetry=False)


def _team(team_id: str, model: FakeChatModel | None = None) -> Team:
    member = _agent(f"{team_id}-membro")
    return Team(
        id=team_id, name=team_id, members=[member], model=model or FakeChatModel(responses=["time"]), telemetry=False
    )


def _body(run_id: str) -> dict[str, Any]:
    return {
        "threadId": f"t-{uuid.uuid4().hex[:8]}",
        "runId": run_id,
        "state": {},
        "messages": [{"id": "m1", "role": "user", "content": "oi"}],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=RUN)


def _tracked_runs() -> dict[str, bool]:
    return get_cancellation_manager().get_active_runs()


async def _wait_registered(run_id: str) -> None:
    for _ in range(500):
        if run_id in _tracked_runs():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"run {run_id} não foi registrado")


# ── run não registrado: 404 e nenhum estado ──────────────────────────


def test_montagem_instala_o_gerenciador_que_so_cancela_run_registrado(monkeypatch: pytest.MonkeyPatch):
    _mount(monkeypatch, [_agent("ag")], [])

    assert isinstance(get_cancellation_manager(), RegisteredRunCancellationManager)


def test_cancel_fica_numa_rota_so_por_entidade(monkeypatch: pytest.MonkeyPatch):
    """A rota do agno é pulada (``preserve_base_app``): só a nossa responde o cancel."""
    app = _mount(monkeypatch, [_agent("ag")], [_team("tm")])

    paths = [r.path for r in app.routes if isinstance(r, APIRoute) and "POST" in r.methods]

    assert paths.count("/agents/{agent_id}/runs/{run_id}/cancel") == 1
    assert paths.count("/teams/{team_id}/runs/{run_id}/cancel") == 1


@pytest.mark.parametrize(
    ("path", "detail"),
    [
        ("/agents/ag/runs/run-inexistente/cancel", "run não encontrado ou já encerrado"),
        ("/teams/tm/runs/run-inexistente/cancel", "run não encontrado ou já encerrado"),
        ("/agents/nao-existe/runs/run-inexistente/cancel", "entidade não encontrada"),
        ("/teams/nao-existe/runs/run-inexistente/cancel", "entidade não encontrada"),
    ],
)
async def test_cancel_de_run_ou_entidade_inexistente_responde_404_sem_criar_estado(
    monkeypatch: pytest.MonkeyPatch, path: str, detail: str
):
    app = _mount(monkeypatch, [_agent("ag")], [_team("tm")])

    async with _client(app) as client:
        response = await client.post(path)

    assert response.status_code == 404
    assert response.json() == {"detail": detail}
    assert _tracked_runs() == {}


async def test_muitos_cancels_de_ids_inventados_nao_crescem_o_gerenciador(monkeypatch: pytest.MonkeyPatch):
    app = _mount(monkeypatch, [_agent("ag")], [])

    async with _client(app) as client:
        statuses = {(await client.post(f"/agents/ag/runs/inventado-{n}/cancel")).status_code for n in range(50)}

    assert statuses == {404}
    assert _tracked_runs() == {}


@pytest.mark.parametrize("entity", ["agente", "team"])
async def test_cancel_antes_do_run_nao_pre_cancela_o_run_com_o_mesmo_run_id(
    monkeypatch: pytest.MonkeyPatch, entity: str
):
    """Ataque: cancelar um ``runId`` previsível antes de a vítima usá-lo no AG-UI."""
    agent = _agent("ag", FakeChatModel(responses=["resposta da vítima"]))
    team = _team("tm", FakeChatModel(responses=["resposta da vítima"]))
    app = _mount(monkeypatch, [agent], [team])
    cancel_path, run_path = (
        ("/agents/ag/runs/run-da-vitima/cancel", "/agui/ag")
        if entity == "agente"
        else ("/teams/tm/runs/run-da-vitima/cancel", "/agui/tm")
    )

    async with _client(app) as client:
        pre = await client.post(cancel_path)
        victim = await client.post(run_path, json=_body("run-da-vitima"))

    assert pre.status_code == 404
    events = parse_agui_sse(victim.text)
    assert_valid_run(events)
    assert isinstance(events[-1], RunFinishedEvent)
    assert text_of(events) == "resposta da vítima"
    assert _tracked_runs() == {}


async def test_cancel_depois_do_fim_do_run_responde_404_e_nao_deixa_resto(monkeypatch: pytest.MonkeyPatch):
    app = _mount(monkeypatch, [_agent("ag")], [])

    async with _client(app) as client:
        finished = await client.post("/agui/ag", json=_body("run-encerrado"))
        late = await client.post("/agents/ag/runs/run-encerrado/cancel")

    assert isinstance(parse_agui_sse(finished.text)[-1], RunFinishedEvent)
    assert late.status_code == 404
    assert _tracked_runs() == {}


# ── run registrado (em andamento): continua cancelável ───────────────


@pytest.mark.parametrize("entity", ["agente", "team"])
async def test_cancel_de_run_em_andamento_encerra_o_run_com_run_cancelled(
    monkeypatch: pytest.MonkeyPatch, entity: str
):
    agent = _agent("tic", _TickingModel(responses=[]))
    team = _team("tm", _TickingModel(responses=[]))
    app = _mount(monkeypatch, [agent], [team])
    run_path, cancel_path = (
        ("/agui/tic", "/agents/tic/runs/run-em-andamento/cancel")
        if entity == "agente"
        else ("/agui/tm", "/teams/tm/runs/run-em-andamento/cancel")
    )

    async with _client(app) as client:
        run = asyncio.create_task(client.post(run_path, json=_body("run-em-andamento")))
        await _wait_registered("run-em-andamento")
        cancel = await client.post(cancel_path)
        response = await asyncio.wait_for(run, timeout=15)

    assert cancel.status_code == 200
    assert cancel.json() == {}
    events = parse_agui_sse(response.text)
    assert_valid_run(events)
    assert isinstance(events[-1], RunErrorEvent)
    assert events[-1].code == "run_cancelled"
    assert _tracked_runs() == {}


async def test_run_id_do_cliente_ja_em_andamento_e_trocado_por_um_do_servidor(monkeypatch: pytest.MonkeyPatch):
    """Dois runs com o mesmo ``run_id`` dividiriam a entrada do gerenciador: o fim de um apagava o
    registro do outro (que deixava de ser cancelável) e um cancel atingia os dois."""
    app = _mount(monkeypatch, [_agent("ag", FakeChatModel(responses=["segundo"]))], [])
    get_cancellation_manager().register_run("run-ocupado")  # run em andamento de outro cliente

    async with _client(app) as client:
        response = await client.post("/agui/ag", json=_body("run-ocupado"))

    events = parse_agui_sse(response.text)
    assert_valid_run(events)
    assert isinstance(events[-1], RunFinishedEvent) and text_of(events) == "segundo"
    started = events[0].run_id  # type: ignore[attr-defined]
    assert started != "run-ocupado" and uuid.UUID(started).version == 4
    assert _tracked_runs() == {"run-ocupado": False}


def test_falha_ao_criar_o_agentos_nao_deixa_rota_de_cancel_parcial(monkeypatch: pytest.MonkeyPatch):
    """O AgentOS recusa ids repetidos no construtor: o app não pode ficar só com as nossas rotas."""
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    factory = AppFactory()
    app = factory.create_app()

    with pytest.raises(ValueError, match="Duplicate IDs"):
        factory._mount_agent_os(app, [_agent("dup"), _agent("dup")], [])

    assert not [r for r in app.routes if isinstance(r, APIRoute) and r.path.endswith("/cancel")]
