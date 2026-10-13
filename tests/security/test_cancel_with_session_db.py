"""F2-08: cancelamento de run com o db de sessões de produção (``SessionDb``, assíncrono).

Mesma cadeia dos testes de cancelamento do F1-10 (AgentOS montado, rotas de cancel próprias, chave
run), agora com as entidades e o AgentOS no ``SessionDb`` sobre o cliente motor em memória: o cancel
continua valendo para run REST e de background, não deixa resto no gerenciador e o run cancelado
fica gravado na sessão do usuário.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.run.cancel import get_cancellation_manager
from fastapi import FastAPI

from src.infrastructure.runtime.agno.session_db import SessionDb
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import FakeChatModel
from tests.fakes.session_db import fake_session_db, session_docs
from tests.fakes.web import mount_agent_os

RUN_KEY = "f208-run-key-" + "r" * 22
ADMIN_KEY = "f208-admin-key-" + "a" * 20
RUN = {"Authorization": f"Bearer {RUN_KEY}"}
_ENV_NAMES = ("ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS", "APP_HOST", "API_KEY_RUN", "API_KEY_ADMIN")


class TickingModel(FakeChatModel):
    """Um trecho a cada 20 ms no stream; sem stream, uma chamada de 0,5 s (o agno checa o
    cancelamento entre trechos e ao voltar da chamada)."""

    async def ainvoke_stream(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def,override]
        for index in range(500):
            yield ModelResponse(role="assistant", content=f"t{index} ")
            await asyncio.sleep(0.02)

    async def ainvoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        await asyncio.sleep(0.5)
        return ModelResponse(role="assistant", content="fim")


@pytest.fixture
def session_db() -> SessionDb:
    return fake_session_db()


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch, session_db: SessionDb) -> FastAPI:
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    monkeypatch.setenv("APP_HOST", "0.0.0" + ".0")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("API_KEY_RUN", RUN_KEY)
    monkeypatch.setenv("API_KEY_ADMIN", ADMIN_KEY)
    agent = Agent(id="tic", name="tic", model=TickingModel(), db=session_db, telemetry=False)
    factory = AppFactory()
    application = factory.create_app()
    mount_agent_os(factory, application, [agent], [], session_db=session_db)
    return application


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=RUN)


def _tracked() -> dict[str, bool]:
    return get_cancellation_manager().get_active_runs()


async def _wait_for(predicate: Any, what: str, limit: float = 10.0) -> None:
    for _ in range(int(limit / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timeout esperando {what}; gerenciador={_tracked()}")


def _stored_runs(db: SessionDb) -> list[tuple[str, str, str]]:
    """``(session_id, user_id, status do run)`` de cada run gravado em ``agno_sessions``."""
    return [
        (doc["session_id"], doc["user_id"], run["status"])
        for doc in session_docs(db)
        for run in doc.get("runs") or []
    ]


@pytest.mark.parametrize("stream", ["false", "true"])
async def test_cancel_de_run_rest_com_db_de_sessoes_cancela_limpa_e_grava_o_run_cancelado(
    app: FastAPI, session_db: SessionDb, stream: str
) -> None:
    form = {"message": "oi", "stream": stream, "user_id": "ana", "session_id": "s-cancel"}
    async with _client(app) as client:
        run = asyncio.create_task(client.post("/agents/tic/runs", data=form))
        await _wait_for(lambda: len(_tracked()) == 1, "um run registrado")
        run_id = next(iter(_tracked()))
        cancel = await client.post(f"/agents/tic/runs/{run_id}/cancel")
        response = await asyncio.wait_for(run, timeout=15)

    assert cancel.status_code == 200 and cancel.json() == {}
    assert response.status_code == 200
    assert ("RunCancelled" in response.text) if stream == "true" else (response.json()["status"] == "CANCELLED")
    assert _tracked() == {}
    assert _stored_runs(session_db) == [("s-cancel", "ana", "CANCELLED")]


async def test_cancel_de_run_de_background_com_db_de_sessoes_cancela_e_o_run_fica_consultavel(
    app: FastAPI, session_db: SessionDb
) -> None:
    async with _client(app) as client:
        started = await client.post(
            "/agents/tic/runs", data={"message": "oi", "background": "true", "user_id": "ana"}
        )
        assert started.status_code == 202, started.text
        body = started.json()
        cancel = await client.post(f"/agents/tic/runs/{body['run_id']}/cancel")
        await _wait_for(lambda: _tracked() == {}, "cleanup do run de background")
        polled = await client.get(f"/agents/tic/runs/{body['run_id']}", params={"session_id": body["session_id"]})

    assert cancel.status_code == 200
    assert polled.status_code == 200 and polled.json()["status"] == "CANCELLED"
    assert _stored_runs(session_db) == [(body["session_id"], "ana", "CANCELLED")]


async def test_cancel_de_run_inexistente_com_db_de_sessoes_e_404_e_nao_guarda_estado(app: FastAPI) -> None:
    async with _client(app) as client:
        response = await client.post("/agents/tic/runs/run-que-nao-existe/cancel")

    assert response.status_code == 404
    assert _tracked() == {}
