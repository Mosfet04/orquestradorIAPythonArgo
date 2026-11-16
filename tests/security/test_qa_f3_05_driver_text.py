"""QA do F3-05: com o Mongo de sessões fora, o texto do erro do driver não pode chegar ao cliente HTTP.

O item registra "REST e AG-UI 500 sem o texto do erro". Vale para as rotas de run e para as rotas
novas do agno 3.x que leem a sessão (primeiro teste). Não vale para rotas do AgentOS que montam a
resposta com ``str(e)`` (segundo teste, ``xfail`` estrito): duas já eram assim no 2.5.8 e três entram
com o bump (``POST /sessions/search`` e os dois ``resume``). O texto do pymongo traz host, porta e
topologia do banco; a convenção do projeto é nunca devolver ``str(exc)`` ao cliente.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from agno.agent import Agent
from agno.team import Team
from fastapi import FastAPI

from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import FakeChatModel
from tests.fakes.mongo import FakeMongoCollection
from tests.fakes.session_db import fake_session_db
from tests.fakes.web import mount_agent_os

RUN_KEY = "f305-qa-run-key-" + "r" * 20
ADMIN_KEY = "f305-qa-admin-key-" + "a" * 20
DRIVER_HOST = "mongo.interno"
DRIVER_MARK = "TEXTO-DO-DRIVER-9931"
_ENV_NAMES = ("ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS", "APP_HOST", "API_KEY_RUN", "API_KEY_ADMIN")
_OPERATIONS = (
    "find",
    "find_one",
    "count_documents",
    "aggregate",
    "replace_one",
    "distinct",
    "find_one_and_replace",
    "find_one_and_update",
    "delete_one",
    "delete_many",
)
_RUN_FORM = {"message": "oi", "stream": "false", "session_id": "s1", "user_id": "ana"}
_SESSION = {"session_id": "s1"}
_AGENT_SESSION = {"type": "agent"}


def _server_down(*_: object, **__: object) -> Any:
    raise ConnectionError(f"sem rota para {DRIVER_HOST}:27017 {DRIVER_MARK}")


@pytest.fixture
def app_with_mongo_down(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    """App de produção montado com o Mongo no ar; depois toda leitura e escrita das coleções falha."""
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    monkeypatch.setenv("APP_HOST", "0.0.0" + ".0")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("API_KEY_RUN", RUN_KEY)
    monkeypatch.setenv("API_KEY_ADMIN", ADMIN_KEY)
    db = fake_session_db()
    agent = Agent(id="ag", name="ag", model=FakeChatModel(responses=["resposta"] * 5), db=db, telemetry=False)
    member = Agent(id="mb", name="mb", model=FakeChatModel(responses=["do membro"] * 5), db=db, telemetry=False)
    team = Team(
        id="tm", name="tm", members=[member], model=FakeChatModel(responses=["do time"] * 5), db=db, telemetry=False
    )
    factory = AppFactory()
    app = factory.create_app()
    mount_agent_os(factory, app, [agent], [team], session_db=db)
    for operation in _OPERATIONS:
        monkeypatch.setattr(FakeMongoCollection, operation, _server_down)
    return app


async def _request(app: FastAPI, method: str, path: str, **kwargs: Any) -> httpx.Response:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    headers = {"Authorization": f"Bearer {RUN_KEY}"}
    async with httpx.AsyncClient(transport=transport, base_url="http://test", headers=headers) as client:
        return await client.request(method, path, **kwargs)


def _assert_no_driver_text(response: httpx.Response) -> None:
    assert DRIVER_MARK not in response.text and DRIVER_HOST not in response.text, response.text[:200]


@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("POST", "/agents/ag/runs", {"data": _RUN_FORM}),
        ("POST", "/agents/ag/runs", {"data": {**_RUN_FORM, "stream": "true"}}),
        ("POST", "/teams/tm/runs", {"data": _RUN_FORM}),
        ("POST", "/agents/ag/runs/r1/continue", {"data": {"tools": "[]", "session_id": "s1", "stream": "false"}}),
        ("GET", "/agents/ag/runs/r1/checkpoints", {"params": _SESSION}),
        ("GET", "/teams/tm/runs/r1/checkpoints", {"params": _SESSION}),
        ("POST", "/agents/ag/sessions/s1/fork", {}),
        ("POST", "/teams/tm/sessions/s1/fork", {}),
        ("POST", "/agents/ag/sessions/s1/compact", {}),
        ("GET", "/sessions/s1/run-previews", {"params": _AGENT_SESSION}),
        ("GET", "/sessions", {"params": _AGENT_SESSION}),
        ("GET", "/sessions/s1", {"params": _AGENT_SESSION}),
        ("GET", "/sessions/s1/runs", {"params": _AGENT_SESSION}),
    ],
    ids=[
        "run-agente",
        "run-agente-sse",
        "run-team",
        "continue",
        "checkpoints-agente",
        "checkpoints-team",
        "fork-agente",
        "fork-team",
        "compact",
        "run-previews",
        "listar-sessoes",
        "ler-sessao",
        "runs-da-sessao",
    ],
)
async def test_rotas_de_run_e_de_sessao_com_mongo_fora_respondem_500_sem_o_texto_do_driver(
    app_with_mongo_down: FastAPI, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    response = await _request(app_with_mongo_down, method, path, **kwargs)

    assert response.status_code == 500
    _assert_no_driver_text(response)


@pytest.mark.xfail(
    strict=True,
    reason="BUG-F305-AGNO-ERROR-TEXT: rotas do AgentOS (classe RUN) devolvem str(e) do driver do Mongo ao "
    "cliente: POST /sessions/search e os dois resume entram com o agno 3.1.12; POST /sessions e "
    "GET /user_memory_stats já eram assim no 2.5.8",
)
@pytest.mark.parametrize(
    ("method", "path", "kwargs"),
    [
        ("POST", "/sessions/search", {"json": {}}),
        ("POST", "/agents/ag/runs/r1/resume", {"data": {"session_id": "s1", "stream": "false"}}),
        ("POST", "/teams/tm/runs/r1/resume", {"data": {"session_id": "s1", "stream": "false"}}),
        ("POST", "/sessions", {"params": _AGENT_SESSION, "json": {"user_id": "ana"}}),
        ("GET", "/user_memory_stats", {}),
    ],
    ids=[
        "novo-no-3x-sessions-search",
        "novo-no-3x-resume-agente",
        "novo-no-3x-resume-team",
        "desde-o-2.5.8-criar-sessao",
        "desde-o-2.5.8-user-memory-stats",
    ],
)
async def test_rotas_do_agentos_com_mongo_fora_nao_devolvem_o_texto_do_driver(
    app_with_mongo_down: FastAPI, method: str, path: str, kwargs: dict[str, Any]
) -> None:
    response = await _request(app_with_mongo_down, method, path, **kwargs)

    _assert_no_driver_text(response)
