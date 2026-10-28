"""QA do FA-06: a config (``TOOL_ROUTE_ALLOWLIST`` e o modo dev local) chega à guarda de destino da ``route``.

App real (lifespan -> ``DependencyContainer`` -> ``build_config_admin`` -> ``ToolRouteGuard``) sobre o
``startup_world`` (Mongo, sessões e modelo falsos; DNS falso público). Nenhum host real é resolvido.
Valores são de teste.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.infrastructure.web import app_factory
from src.presentation.routes.admin_config import router as admin_config_router
from tests.fakes.admin_config_app import ADMIN, admin_env, tool_body
from tests.fakes.startup_world import Events, assign_seed_ids, startup_world
from tests.fakes.web import loopback_client

BASE = "/admin/config/tools"
REF = {"Authorization": "env:CEP_API_KEY"}
INVALID_ROUTE = {"detail": "Configuração inválida", "field": "route"}
INVALID_HEADERS = {"detail": "Configuração inválida", "field": "headers"}


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    admin_env(monkeypatch)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")


def _post(http: Any, route: str, headers: dict[str, str] | None = None, tool_id: str = "cep") -> Any:
    body = tool_body(id=tool_id, name=tool_id, route=route, **({"headers": headers} if headers else {}))
    return http.post(BASE, json=body, headers=ADMIN)


@pytest.mark.usefixtures("offline_knowledge")
def test_allowlist_do_operador_vale_na_pilha_real(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("TOOL_ROUTE_ALLOWLIST", "API.Interna.example., 10.0.0.5")
    with startup_world(Events(), agents=("a1",)):
        assign_seed_ids()
        app = app_factory.AppFactory().create_app(routers=[admin_config_router])
        with loopback_client(app) as http:
            responses = {
                "host da allowlist + referência": _post(http, "https://api.interna.example/cep", REF, "t1"),
                "ip privado da allowlist": _post(http, "http://10.0.0.5:9000/cep", REF, "t2"),
                "outro ip privado": _post(http, "http://10.0.0.6/cep", None, "t3"),
                "metadata": _post(http, "http://169.254.169.254/cep", None, "t4"),
                "loopback em produção": _post(http, "http://127.0.0.1:8000/cep", None, "t5"),
                "público + referência": _post(http, "https://publico.example.invalid/cep", REF, "t6"),
                "público + texto": _post(http, "https://publico.example.invalid/cep", {"X-Nome": "valor"}, "t7"),
                "sensível em texto": _post(http, "https://api.interna.example/cep", {"Authorization": "x"}, "t8"),
            }
            shown = http.get(f"{BASE}/t1", headers=ADMIN)

    assert {name: r.status_code for name, r in responses.items()} == {
        "host da allowlist + referência": 201,
        "ip privado da allowlist": 201,
        "outro ip privado": 422,
        "metadata": 422,
        "loopback em produção": 422,
        "público + referência": 422,
        "público + texto": 201,
        "sensível em texto": 422,
    }
    assert responses["outro ip privado"].json() == INVALID_ROUTE
    assert responses["público + referência"].json() == INVALID_HEADERS
    assert shown.json()["headers"] == REF


@pytest.mark.usefixtures("offline_knowledge")
def test_sem_allowlist_nenhum_ip_privado_e_nenhuma_referencia_passam_pela_api(monkeypatch: pytest.MonkeyPatch):
    with startup_world(Events(), agents=("a1",)):
        assign_seed_ids()
        app = app_factory.AppFactory().create_app(routers=[admin_config_router])
        with loopback_client(app) as http:
            private = _post(http, "http://10.0.0.5/cep", None, "t1")
            ref = _post(http, "https://api.interna.example/cep", REF, "t2")
            plain = _post(http, "https://api.interna.example/cep", None, "t3")

    assert (private.status_code, private.json()) == (422, INVALID_ROUTE)
    assert (ref.status_code, ref.json()) == (422, INVALID_HEADERS)
    assert plain.status_code == 201


@pytest.mark.usefixtures("offline_knowledge")
@pytest.mark.parametrize(
    ("with_keys", "expected"),
    [(False, 201), (True, 422)],
    ids=["dev local (sem chaves, loopback)", "development com chaves configuradas"],
)
def test_loopback_so_passa_pela_api_no_modo_dev_local(monkeypatch: pytest.MonkeyPatch, with_keys: bool, expected: int):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("APP_HOST", "127.0.0.1")
    if not with_keys:
        for name in ("API_KEY_RUN", "API_KEY_ADMIN"):
            monkeypatch.delenv(name)
    with startup_world(Events(), agents=("a1",)):
        assign_seed_ids()
        app = app_factory.AppFactory().create_app(routers=[admin_config_router])
        with loopback_client(app) as http:
            response = _post(http, "http://127.0.0.1:8000/cep")

    assert response.status_code == expected
    if expected == 422:
        assert response.json() == INVALID_ROUTE
