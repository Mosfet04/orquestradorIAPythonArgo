"""QA do F3-08: ``/eval-runs*`` pela pilha real (auth da borda + AgentOS montado), sem egress.

O teste do dev (``test_agno_telemetry.py``) prova o 404 sem telemetria com o modo dev local. Aqui:
a ordem auth -> 404 (a rota desligada não é enumerável sem chave admin), entradas hostis (corpo,
query, variações de path, métodos fora da lista) e que nenhuma delas roda o agente nem toca a rede.

O ``network.attempts == []`` daqui é checado sem drenar a fila de telemetria em background; a prova
com drenagem (evento de eval de controle) é
``test_agno_telemetry.py::test_eval_runs_responde_404_sem_egress_para_a_telemetria``.
As chaves são valores de teste, não segredos.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from agno.agent import Agent
from fastapi import FastAPI
from fastapi.routing import APIRoute
from starlette.testclient import TestClient

from src.infrastructure.runtime.agno.eval_runs_router import EVAL_RUNS_ROUTES
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import FakeChatModel
from tests.fakes.network import NetworkAttempts, block_network
from tests.fakes.web import mount_agent_os

pytestmark = pytest.mark.security

RUN_KEY = "chave-de-teste-run-" + "r" * 21
ADMIN_KEY = "chave-de-teste-admin-" + "a" * 19
NOT_FOUND = {"detail": "recurso não encontrado"}
ROUTES_FILLED = [(method, path.replace("{eval_run_id}", "er-1")) for method, path in EVAL_RUNS_ROUTES]
ROUTE_IDS = [f"{method} {path}" for method, path in ROUTES_FILLED]
HOSTILE_BODY = '{"agent_id": "ag", "eval_type": "performance", "input": "oi", "num_iterations": 1}'


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


@pytest.fixture(scope="module")
def prod_app() -> Iterator[tuple[FastAPI, Agent]]:
    """App de produção (chaves run/admin) com o AgentOS montado; montado uma vez por módulo."""
    agent = Agent(id="ag", name="ag", model=FakeChatModel(responses=["ok"] * 5), telemetry=False)
    with pytest.MonkeyPatch.context() as mp:
        for name in ("ENABLE_DOCS", "CORS_ALLOWED_ORIGINS"):
            mp.delenv(name, raising=False)
        mp.setenv("AGNO_TELEMETRY", "false")
        mp.setenv("ENVIRONMENT", "production")
        mp.setenv("APP_HOST", "0.0.0.0")  # noqa: S104
        mp.setenv("API_KEY_RUN", RUN_KEY)
        mp.setenv("API_KEY_ADMIN", ADMIN_KEY)
        mp.setenv("CORS_ALLOWED_ORIGINS", "https://painel.example.com")
        factory = AppFactory()
        app = factory.create_app()
        mount_agent_os(factory, app, [agent], [])
    yield app, agent
    assert agent.model.calls == [], "request a /eval-runs* chegou ao modelo"  # type: ignore[union-attr]


@pytest.fixture
def client(prod_app: tuple[FastAPI, Agent]) -> TestClient:
    return TestClient(prod_app[0])


@pytest.fixture
def network(monkeypatch: pytest.MonkeyPatch) -> NetworkAttempts:
    return block_network(monkeypatch)


@pytest.mark.parametrize(("method", "path"), ROUTES_FILLED, ids=ROUTE_IDS)
def test_admin_recebe_404_generico_sem_egress(client: TestClient, network: NetworkAttempts, method: str, path: str):
    response = client.request(method, path, headers=_bearer(ADMIN_KEY), content=HOSTILE_BODY)

    assert response.status_code == 404
    assert response.json() == NOT_FOUND
    assert network.attempts == []


@pytest.mark.parametrize(("method", "path"), ROUTES_FILLED, ids=ROUTE_IDS)
def test_auth_responde_antes_do_404_da_rota_desligada(client: TestClient, method: str, path: str):
    """A auth responde antes do 404: sem chave admin a rota desligada não aparece."""
    run = client.request(method, path, headers=_bearer(RUN_KEY), content=HOSTILE_BODY)
    anonymous = client.request(method, path, content=HOSTILE_BODY)

    assert (run.status_code, run.json()) == (403, {"detail": "forbidden"})
    assert (anonymous.status_code, anonymous.json()) == (401, {"detail": "unauthorized"})


@pytest.mark.parametrize(
    ("headers", "content", "query"),
    [
        ({"Content-Type": "application/json"}, b"{nao-e-json", ""),
        ({"Content-Type": "application/json"}, b"[]", ""),
        ({"Content-Type": "multipart/form-data; boundary=x"}, b"--x\r\n", ""),
        ({"Content-Type": "application/x-www-form-urlencoded"}, b"agent_id=ag&eval_type=performance", ""),
        ({"Content-Type": "application/json"}, b"x" * 2_000_000, ""),
        ({}, b"", "?limit=-1&page=abc&agent_id=%00&eval_type=../../etc/passwd"),
    ],
    ids=["json-quebrado", "json-lista", "multipart-quebrado", "form", "corpo-2mb", "query-hostil"],
)
def test_corpo_e_query_hostis_nao_sao_lidos_nem_validados(
    client: TestClient, network: NetworkAttempts, headers: dict[str, str], content: bytes, query: str
):
    """Nada de 422 nem 5xx: o handler não lê corpo nem parâmetro; sempre o mesmo 404."""
    for method, path in ROUTES_FILLED:
        response = client.request(method, path + query, headers={**_bearer(ADMIN_KEY), **headers}, content=content)

        assert response.status_code == 404, (method, path, response.text[:200])
        assert response.json() == NOT_FOUND
    assert network.attempts == []


@pytest.mark.parametrize(
    "path",
    ["/eval-runs/", "/eval-runs/../eval-runs", "/%65val-runs", "/eval-runs%2Fx", "/eval-runs/%2e%2e", "/eval-runs/a/b"],
)
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE", "PATCH"])
def test_variacoes_de_path_nunca_chegam_ao_eval_do_agno(
    client: TestClient, network: NetworkAttempts, method: str, path: str
):
    """Barra final, encoding e ``..`` com chave admin: 404 (da rota ou do roteador); nunca 2xx/5xx."""
    response = client.request(method, path, headers=_bearer(ADMIN_KEY), content=HOSTILE_BODY, follow_redirects=True)

    assert response.status_code in (404, 405), (response.status_code, response.text[:200])
    assert network.attempts == []


@pytest.mark.parametrize("path", ["/eval-runs", "/eval-runs/er-1"])
@pytest.mark.parametrize("method", ["PUT", "HEAD", "OPTIONS", "TRACE"])
def test_metodo_fora_da_lista_nao_executa_eval(client: TestClient, network: NetworkAttempts, method: str, path: str):
    response = client.request(method, path, headers=_bearer(ADMIN_KEY), content=HOSTILE_BODY)

    assert response.status_code in (404, 405), (response.status_code, response.text[:200])
    assert network.attempts == []


def test_cada_rota_de_eval_runs_e_a_do_modulo_novo_e_fora_do_openapi(prod_app: tuple[FastAPI, Agent]):
    """Quem atende ``/eval-runs*`` é o handler do 404, não o do agno (R1 do review)."""
    app = prod_app[0]
    routes = [r for r in app.routes if isinstance(r, APIRoute) and r.path.startswith("/eval-runs")]

    assert sorted((m, r.path) for r in routes for m in r.methods) == sorted(EVAL_RUNS_ROUTES)
    assert {r.endpoint.__module__ for r in routes} == {"src.infrastructure.runtime.agno.eval_runs_router"}
    assert all(not r.include_in_schema for r in routes)
