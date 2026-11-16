"""QA do F3-05: as rotas que o AgentOS 3.1.12 trouxe, pela borda real (app montado com as chaves run e admin).

O ``test_qa_f3_04_edge.py`` prova o middleware com um app de mentira e o snapshot de rotas prova a
tabela; aqui, o app real no 3.1.12: a chave run recebe 403 nas rotas de operador novas (fila, contas de
serviço, filesystem, learnings, métricas), o handler do agno nem é alcançado, e a chave admin passa.
"""

from __future__ import annotations

import httpx
import pytest
from agno.agent import Agent
from fastapi import FastAPI
from starlette.routing import Match

from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import FakeChatModel
from tests.fakes.session_db import fake_session_db
from tests.fakes.web import mount_agent_os

RUN_KEY = "f305-qa-run-key-" + "r" * 20
ADMIN_KEY = "f305-qa-admin-key-" + "a" * 20
_ENV_NAMES = ("ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS", "APP_HOST", "API_KEY_RUN", "API_KEY_ADMIN")

# Rotas de operador que o agno 3.1.12 acrescentou (método, caminho); o corpo é inócuo de propósito.
NEW_ADMIN_ROUTES = [
    ("GET", "/queue"),
    ("POST", "/queue/dlq/requeue"),
    ("DELETE", "/queue/dlq/abc"),
    ("GET", "/service-accounts"),
    ("POST", "/service-accounts"),
    ("DELETE", "/service-accounts/sa-1"),
    ("GET", "/filesystem/files"),
    ("GET", "/filesystem/entries"),
    ("GET", "/filesystem/content"),
    ("GET", "/filesystem/search"),
    ("GET", "/learnings"),
    ("POST", "/learnings"),
    ("GET", "/learnings/users"),
    ("DELETE", "/learnings/users/ana"),
    ("GET", "/learnings/l-1"),
    ("PATCH", "/learnings/l-1"),
    ("DELETE", "/learnings/l-1"),
    ("GET", "/metrics/refresh/status"),
    ("POST", "/knowledge/content/c-1/refresh"),
]


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch) -> FastAPI:
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    monkeypatch.setenv("APP_HOST", "0.0.0" + ".0")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("API_KEY_RUN", RUN_KEY)
    monkeypatch.setenv("API_KEY_ADMIN", ADMIN_KEY)
    db = fake_session_db()
    agent = Agent(id="ag", name="ag", model=FakeChatModel(responses=["ok"]), db=db, telemetry=False)
    factory = AppFactory()
    app = factory.create_app()
    mount_agent_os(factory, app, [agent], [], session_db=db)
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, raise_app_exceptions=False)
    return httpx.AsyncClient(transport=transport, base_url="http://test")


@pytest.mark.parametrize(("method", "path"), NEW_ADMIN_ROUTES)
async def test_chave_run_nas_rotas_de_operador_do_3x_e_403_e_a_chave_admin_passa(
    app: FastAPI, method: str, path: str
) -> None:
    async with _client(app) as client:
        anonymous = await client.request(method, path)
        as_run = await client.request(method, path, headers={"Authorization": f"Bearer {RUN_KEY}"}, json={})
        as_admin = await client.request(method, path, headers={"Authorization": f"Bearer {ADMIN_KEY}"}, json={})

    assert anonymous.status_code == 401
    assert (as_run.status_code, as_run.json()) == (403, {"detail": "forbidden"})
    assert as_admin.status_code not in (401, 403), as_admin.text  # a borda deixa passar; o handler decide


def test_toda_rota_nova_de_operador_existe_no_app_montado(app: FastAPI) -> None:
    """Se um upgrade renomear uma rota, o 403 acima passaria sem provar nada: cada par da lista
    precisa casar (``Match.FULL``, como o roteador faz) com uma rota real do app montado."""
    missing = [
        (method, path)
        for method, path in NEW_ADMIN_ROUTES
        if not any(
            route.matches({"type": "http", "method": method, "path": path})[0] == Match.FULL for route in app.routes
        )
    ]

    assert missing == []
