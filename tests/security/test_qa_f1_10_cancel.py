"""QA F1-10: cancelamento por REST de run REST real, corridas, cleanup e team.

Complementa ``test_run_cancellation.py`` (que cobre o AG-UI e o 404 de run inexistente).
Aqui: o run é o do próprio REST (``POST /agents|teams/{id}/runs``), em stream, não-stream e
``background=true``; cancel duplo; cancel concorrente com o fim do run; 100 runs mistos sem
resto no gerenciador; team x run de membro. Pilha real (``AppFactory`` + AgentOS + AG-UI),
modelo = ``FakeChatModel``; sem rede e sem Mongo.
"""

from __future__ import annotations

import asyncio
import json
import uuid
from typing import Any

import httpx
import pytest
from ag_ui.core import RunErrorEvent, RunFinishedEvent
from agno.agent import Agent
from agno.db.in_memory import InMemoryDb
from agno.models.response import ModelResponse
from agno.run.cancel import get_cancellation_manager
from agno.team import Team
from fastapi import FastAPI

from src.infrastructure.web.app_factory import AppFactory
from src.infrastructure.web.run_cancellation import RegisteredRunCancellationManager
from tests.fakes import FakeChatModel
from tests.fakes.agui import parse_agui_sse

RUN_KEY = "f110qa-run-key-" + "r" * 20
ADMIN_KEY = "f110qa-admin-key-" + "a" * 18
RUN = {"Authorization": f"Bearer {RUN_KEY}"}
_ENV_NAMES = ("ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS", "APP_HOST", "API_KEY_RUN", "API_KEY_ADMIN")


class _TickingModel(FakeChatModel):
    """Um trecho a cada 20 ms (até 500): o agno checa o cancelamento entre trechos."""

    async def ainvoke_stream(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def,override]
        for index in range(500):
            yield ModelResponse(role="assistant", content=f"t{index} ")
            await asyncio.sleep(0.02)

    async def ainvoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        # Chamada única, sem ponto de cancelamento no meio: o agno só checa ao voltar dela.
        await asyncio.sleep(0.5)
        return ModelResponse(role="assistant", content="fim")


def _mount(monkeypatch: pytest.MonkeyPatch, agents: list[Agent], teams: list[Team]) -> FastAPI:
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    env = {"APP_HOST": "0.0.0" + ".0", "ENVIRONMENT": "production", "API_KEY_RUN": RUN_KEY, "API_KEY_ADMIN": ADMIN_KEY}
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    factory = AppFactory()
    app = factory.create_app()
    factory._mount_agent_os(app, agents, teams)
    return app


def _agent(agent_id: str, model: FakeChatModel | None = None, db: InMemoryDb | None = None) -> Agent:
    return Agent(
        id=agent_id, name=agent_id, model=model or FakeChatModel(responses=["oi"]), telemetry=False, db=db
    )


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=RUN)


def _tracked() -> dict[str, bool]:
    return get_cancellation_manager().get_active_runs()


def _agui_body(run_id: str) -> dict[str, Any]:
    return {
        "threadId": f"t-{uuid.uuid4().hex[:8]}",
        "runId": run_id,
        "state": {},
        "messages": [{"id": "m1", "role": "user", "content": "oi"}],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }


async def _wait_for(predicate: Any, what: str, limit: float = 10.0) -> None:
    for _ in range(int(limit / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timeout esperando {what}; gerenciador={_tracked()}")


async def _wait_one_run() -> str:
    await _wait_for(lambda: len(_tracked()) == 1, "um run registrado")
    return next(iter(_tracked()))


# ── REST: stream / não-stream / background ───────────────────────────


async def test_cancel_via_rest_de_run_rest_em_stream_encerra_com_run_cancelled_e_limpa(
    monkeypatch: pytest.MonkeyPatch,
):
    app = _mount(monkeypatch, [_agent("tic", _TickingModel())], [])

    async with _client(app) as client:
        run = asyncio.create_task(client.post("/agents/tic/runs", data={"message": "oi", "stream": "true"}))
        run_id = await _wait_one_run()
        cancel = await client.post(f"/agents/tic/runs/{run_id}/cancel")
        response = await asyncio.wait_for(run, timeout=15)

    assert cancel.status_code == 200 and cancel.json() == {}
    assert "RunCancelled" in response.text
    assert run_id in response.text
    assert _tracked() == {}


async def test_cancel_via_rest_de_run_rest_nao_stream_devolve_status_cancelado_e_limpa(
    monkeypatch: pytest.MonkeyPatch,
):
    app = _mount(monkeypatch, [_agent("tic", _TickingModel())], [])

    async with _client(app) as client:
        run = asyncio.create_task(client.post("/agents/tic/runs", data={"message": "oi", "stream": "false"}))
        run_id = await _wait_one_run()
        cancel = await client.post(f"/agents/tic/runs/{run_id}/cancel")
        response = await asyncio.wait_for(run, timeout=15)

    assert cancel.status_code == 200
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"
    assert response.json()["run_id"] == run_id
    assert _tracked() == {}


async def test_cancel_via_rest_de_run_de_team_nao_stream_e_stream(monkeypatch: pytest.MonkeyPatch):
    member = _agent("membro")
    team = Team(id="tm", name="tm", members=[member], model=_TickingModel(), telemetry=False)
    app = _mount(monkeypatch, [member], [team])

    async with _client(app) as client:
        for stream in ("false", "true"):
            run = asyncio.create_task(client.post("/teams/tm/runs", data={"message": "oi", "stream": stream}))
            run_id = await _wait_one_run()
            cancel = await client.post(f"/teams/tm/runs/{run_id}/cancel")
            response = await asyncio.wait_for(run, timeout=15)
            assert cancel.status_code == 200, stream
            assert "ancel" in response.text, (stream, response.text[:300])
            assert _tracked() == {}, stream


async def test_cancel_de_run_background_registrado_antes_da_task_cancela_e_limpa(monkeypatch: pytest.MonkeyPatch):
    """``background=true``: 202 com o run_id; o cancel logo depois tem de pegar o run."""
    db = InMemoryDb()
    app = _mount(monkeypatch, [_agent("bg", _TickingModel(), db=db)], [])

    async with _client(app) as client:
        started = await client.post("/agents/bg/runs", data={"message": "oi", "background": "true"})
        assert started.status_code == 202, started.text
        body = started.json()
        cancel = await client.post(f"/agents/bg/runs/{body['run_id']}/cancel")
        await _wait_for(lambda: _tracked() == {}, "cleanup do run de background")
        polled = await client.get(f"/agents/bg/runs/{body['run_id']}", params={"session_id": body["session_id"]})

    assert cancel.status_code == 200, "run de background registrado antes da task tem de ser cancelável"
    assert polled.status_code == 200
    assert polled.json()["status"] == "CANCELLED"


async def test_run_background_que_termina_sozinho_nao_deixa_resto(monkeypatch: pytest.MonkeyPatch):
    db = InMemoryDb()
    app = _mount(monkeypatch, [_agent("bg", FakeChatModel(responses=["pronto"]), db=db)], [])

    async with _client(app) as client:
        started = await client.post("/agents/bg/runs", data={"message": "oi", "background": "true"})
        assert started.status_code == 202
        await _wait_for(lambda: _tracked() == {}, "cleanup do run de background")
        late = await client.post(f"/agents/bg/runs/{started.json()['run_id']}/cancel")

    assert late.status_code == 404
    assert _tracked() == {}


# ── cancel duplo e corrida com o fim do run ──────────────────────────


async def test_cancel_duplo_nao_quebra_e_nao_deixa_resto(monkeypatch: pytest.MonkeyPatch):
    app = _mount(monkeypatch, [_agent("tic", _TickingModel())], [])

    async with _client(app) as client:
        run = asyncio.create_task(client.post("/agents/tic/runs", data={"message": "oi", "stream": "false"}))
        run_id = await _wait_one_run()
        first, second = await asyncio.gather(
            client.post(f"/agents/tic/runs/{run_id}/cancel"), client.post(f"/agents/tic/runs/{run_id}/cancel")
        )
        third = await client.post(f"/agents/tic/runs/{run_id}/cancel")
        await asyncio.wait_for(run, timeout=15)
        last = await client.post(f"/agents/tic/runs/{run_id}/cancel")

    assert first.status_code == 200 or second.status_code == 200
    assert {first.status_code, second.status_code, third.status_code} <= {200, 404}
    assert last.status_code == 404
    assert _tracked() == {}


async def test_cancel_concorrente_com_o_fim_do_run_responde_200_ou_404_nunca_500(monkeypatch: pytest.MonkeyPatch):
    """Rajada de cancels enquanto o run (rápido) começa e termina: sem 500, sem resto."""
    app = _mount(monkeypatch, [_agent("rapido", FakeChatModel(responses=["a"] * 40))], [])
    statuses: list[int] = []
    stop = asyncio.Event()

    async def hammer(client: httpx.AsyncClient, run_id: str) -> None:
        while not stop.is_set():
            statuses.append((await client.post(f"/agents/rapido/runs/{run_id}/cancel")).status_code)
            await asyncio.sleep(0)

    async with _client(app) as client:
        outcomes: list[str] = []
        for n in range(30):
            run_id = f"corrida-{n}"
            stop.clear()
            spam = asyncio.create_task(hammer(client, run_id))
            response = await client.post("/agui/rapido", json=_agui_body(run_id))
            stop.set()
            await spam
            last = parse_agui_sse(response.text)[-1]
            assert isinstance(last, (RunFinishedEvent, RunErrorEvent)), response.text[:300]
            outcomes.append("ok" if isinstance(last, RunFinishedEvent) else last.code or "erro")

    assert set(statuses) <= {200, 404}, set(statuses)
    assert set(outcomes) <= {"ok", "run_cancelled"}, set(outcomes)
    assert _tracked() == {}


# ── cleanup: 100 runs mistos ─────────────────────────────────────────


async def test_cem_runs_mistos_sucesso_erro_e_cancelado_nao_deixam_nada_no_gerenciador(
    monkeypatch: pytest.MonkeyPatch,
):
    ok = _agent("ok", FakeChatModel(responses=["oi"] * 100))
    boom = _agent("boom", FakeChatModel(responses=[]))  # roteiro vazio: o run falha
    tic = _agent("tic", _TickingModel())
    app = _mount(monkeypatch, [ok, boom, tic], [])

    async def one(client: httpx.AsyncClient, n: int) -> str:
        kind = ("ok", "boom", "tic")[n % 3]
        run_id = f"misto-{n}"
        task = asyncio.create_task(client.post(f"/agui/{kind}", json=_agui_body(run_id)))
        if kind == "tic":
            await _wait_for(lambda: run_id in _tracked(), f"registro de {run_id}")
            assert (await client.post(f"/agents/tic/runs/{run_id}/cancel")).status_code == 200
        last = parse_agui_sse((await asyncio.wait_for(task, timeout=60)).text)[-1]
        if isinstance(last, RunFinishedEvent):
            return "ok"
        assert isinstance(last, RunErrorEvent)
        return last.code or "erro"

    async with _client(app) as client:
        results = await asyncio.gather(*(one(client, n) for n in range(100)))

    assert results.count("ok") == 34
    assert results.count("run_cancelled") == 33
    assert results.count("run_error") == 33
    assert _tracked() == {}


async def test_runs_rest_com_erro_nao_deixam_resto(monkeypatch: pytest.MonkeyPatch):
    app = _mount(monkeypatch, [_agent("boom", FakeChatModel(responses=[]))], [])

    async with _client(app) as client:
        for stream in ("true", "false"):
            await client.post("/agents/boom/runs", data={"message": "oi", "stream": stream})

    assert _tracked() == {}


# ── team x run de membro ─────────────────────────────────────────────


def _delegate(member_id: str) -> ModelResponse:
    return ModelResponse(
        role="assistant",
        tool_calls=[
            {
                "id": "call-1",
                "type": "function",
                "function": {
                    "name": "delegate_task_to_member",
                    "arguments": json.dumps({"member_id": member_id, "task": "x"}),
                },
            }
        ],
    )


class _ShortTickingModel(_TickingModel):
    """Como ``_TickingModel``, mas termina sozinho em ~2 s (200 trechos de 10 ms)."""

    async def ainvoke_stream(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def,override]
        for index in range(200):
            yield ModelResponse(role="assistant", content=f"t{index} ")
            await asyncio.sleep(0.01)


def _team_with_member(model: FakeChatModel) -> tuple[Agent, Team]:
    member = _agent("membro", model)
    team = Team(
        id="tm",
        name="tm",
        members=[member],
        model=FakeChatModel(responses=[_delegate("membro"), "o time seguiu"]),
        telemetry=False,
    )
    return member, team


async def test_cancel_do_run_do_team_com_membro_em_andamento_encerra_o_team_e_limpa_ao_fim_do_membro(
    monkeypatch: pytest.MonkeyPatch,
):
    member, team = _team_with_member(_ShortTickingModel())
    app = _mount(monkeypatch, [member], [team])

    async with _client(app) as client:
        run = asyncio.create_task(client.post("/agui/tm", json=_agui_body("run-team")))
        await _wait_for(lambda: len(_tracked()) == 2, "run do team e do membro registrados")
        cancel = await client.post("/teams/tm/runs/run-team/cancel")
        response = await asyncio.wait_for(run, timeout=20)
        # o membro tem outro run_id e segue até o fim (comportamento do agno, ver xfail abaixo);
        # ao terminar, não pode sobrar nada
        await _wait_for(lambda: _tracked() == {}, "cleanup do team e do membro", limit=15)

    assert cancel.status_code == 200
    last = parse_agui_sse(response.text)[-1]
    assert isinstance(last, RunErrorEvent) and last.code == "run_cancelled"


@pytest.mark.xfail(
    strict=True,
    reason="BUG-F110-TEAM-CANCEL-MEMBER: cancelar o run do team não cancela o run do membro (comportamento "
    "do agno 2.5.8, idêntico com o gerenciador stock); o membro segue consumindo o modelo até o fim",
)
async def test_cancel_do_run_do_team_cancela_tambem_o_run_do_membro(monkeypatch: pytest.MonkeyPatch):
    member, team = _team_with_member(_TickingModel())  # 10 s se ninguém o cancelar
    app = _mount(monkeypatch, [member], [team])

    async with _client(app) as client:
        run = asyncio.create_task(client.post("/agui/tm", json=_agui_body("run-team")))
        await _wait_for(lambda: len(_tracked()) == 2, "run do team e do membro registrados")
        await client.post("/teams/tm/runs/run-team/cancel")
        await asyncio.wait_for(run, timeout=20)
        await _wait_for(lambda: _tracked() == {}, "cleanup do membro", limit=2)


async def test_cancel_do_run_do_membro_nao_encerra_o_team(monkeypatch: pytest.MonkeyPatch):
    member, team = _team_with_member(_ShortTickingModel())
    app = _mount(monkeypatch, [member], [team])

    async with _client(app) as client:
        run = asyncio.create_task(client.post("/agui/tm", json=_agui_body("run-team")))
        await _wait_for(lambda: len(_tracked()) == 2, "run do team e do membro registrados")
        member_run = next(r for r in _tracked() if r != "run-team")
        cancel = await client.post(f"/agents/membro/runs/{member_run}/cancel")
        response = await asyncio.wait_for(run, timeout=20)

    assert cancel.status_code == 200
    events = parse_agui_sse(response.text)
    assert isinstance(events[-1], RunFinishedEvent)


@pytest.mark.xfail(
    strict=True,
    reason="BUG-F110-MEMBER-CANCEL-LEAK: o run de membro cancelado fica no gerenciador para sempre "
    "(o agno não faz cleanup no caminho de delegação; idêntico com o gerenciador stock)",
)
async def test_cancel_do_run_do_membro_nao_deixa_resto_no_gerenciador(monkeypatch: pytest.MonkeyPatch):
    member, team = _team_with_member(_ShortTickingModel())
    app = _mount(monkeypatch, [member], [team])

    async with _client(app) as client:
        run = asyncio.create_task(client.post("/agui/tm", json=_agui_body("run-team")))
        await _wait_for(lambda: len(_tracked()) == 2, "run do team e do membro registrados")
        member_run = next(r for r in _tracked() if r != "run-team")
        await client.post(f"/agents/membro/runs/{member_run}/cancel")
        await asyncio.wait_for(run, timeout=20)
        await asyncio.sleep(0.3)

    assert _tracked() == {}


def test_manager_instalado_e_o_registered_apos_montagem(monkeypatch: pytest.MonkeyPatch):
    _mount(monkeypatch, [_agent("a")], [])
    assert isinstance(get_cancellation_manager(), RegisteredRunCancellationManager)


async def test_montagens_repetidas_nao_trocam_o_gerenciador_com_run_em_andamento(monkeypatch: pytest.MonkeyPatch):
    """``install`` é idempotente: remontar (refresh/lifespan repetido) não perde runs registrados."""
    app = _mount(monkeypatch, [_agent("tic", _TickingModel())], [])
    manager = get_cancellation_manager()

    async with _client(app) as client:
        run = asyncio.create_task(client.post("/agents/tic/runs", data={"message": "oi", "stream": "false"}))
        run_id = await _wait_one_run()
        _mount(monkeypatch, [_agent("outro")], [])  # outra montagem enquanto o run corre
        assert get_cancellation_manager() is manager
        cancel = await client.post(f"/agents/tic/runs/{run_id}/cancel")
        await asyncio.wait_for(run, timeout=15)

    assert cancel.status_code == 200
    assert _tracked() == {}
