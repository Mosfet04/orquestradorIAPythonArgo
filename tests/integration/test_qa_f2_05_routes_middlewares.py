"""QA do F2-05: rotas, classe de auth e pilha de middlewares iguais às da base ``811d082``.

O snapshot (``snapshots/f2_05_routes_middlewares.json``) foi gerado exportando a base com
``git archive 811d082`` e montando o mesmo app (1 agente com db, 1 membro, 1 team) com o
``AppFactory._mount_agent_os`` de lá; a única diferença deliberada do F2-05 é ``/approvals*`` passar
de ``RUN`` para ``ADMIN`` (editada à mão no snapshot). Ordem das rotas conta (casamento por ordem).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from agno.agent import Agent
from agno.db.in_memory import InMemoryDb
from agno.team import Team

from src.infrastructure.web.api_key_auth import classify_route
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import FakeChatModel
from tests.fakes.web import mount_agent_os

SNAPSHOT = json.loads((Path(__file__).parent / "snapshots" / "f2_05_routes_middlewares.json").read_text())
ENV_NAMES = ("API_KEY_RUN", "API_KEY_ADMIN", "APP_HOST", "ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS")
ENVIRONMENTS = {
    "dev": {},
    "prod": {
        "ENVIRONMENT": "production",
        "API_KEY_RUN": "r" * 40,
        "API_KEY_ADMIN": "a" * 40,
        "ENABLE_DOCS": "true",
        "CORS_ALLOWED_ORIGINS": "https://x.example.com",
    },
}


def _mounted_app(monkeypatch: pytest.MonkeyPatch, env: dict[str, str]) -> Any:
    for name in ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    monkeypatch.setenv("OTEL_ENABLED", "false")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    factory = AppFactory()
    app = factory.create_app()
    agent = Agent(id="a1", name="A1", model=FakeChatModel(responses=["x"]), telemetry=False, db=InMemoryDb())
    member = Agent(id="m1", name="M1", model=FakeChatModel(responses=["x"]), telemetry=False)
    team = Team(id="t1", name="T1", members=[member], model=FakeChatModel(responses=["x"]), telemetry=False)
    mount_agent_os(factory, app, [agent, member], [team])
    return app


@pytest.mark.parametrize("env_name", ["dev", "prod"])
def test_tabela_de_rotas_metodos_e_classe_de_auth_igual_a_base(
    monkeypatch: pytest.MonkeyPatch, env_name: str
) -> None:
    app = _mounted_app(monkeypatch, ENVIRONMENTS[env_name])
    docs_admin = env_name == "prod"

    rows: list[list[Any]] = []
    for route in app.routes:
        path = getattr(route, "path", None) or getattr(route, "path_format", str(route))
        methods = sorted(getattr(route, "methods", None) or ["<ws/mount>"])
        access = {
            m: classify_route(m, path, docs_require_admin=docs_admin).name
            for m in (methods if methods != ["<ws/mount>"] else ["GET"])
        }
        rows.append([type(route).__name__, path, methods, access])

    assert rows == SNAPSHOT[env_name]["routes"]


@pytest.mark.parametrize("env_name", ["dev", "prod"])
def test_pilha_de_middlewares_e_cors_igual_a_base(monkeypatch: pytest.MonkeyPatch, env_name: str) -> None:
    app = _mounted_app(monkeypatch, ENVIRONMENTS[env_name])

    app.middleware_stack = None
    layer: Any = app.build_middleware_stack()
    chain = []
    while layer is not None:
        chain.append(type(layer).__name__)
        layer = getattr(layer, "app", None)
    cors = [
        {
            k: v if not isinstance(v, list | tuple | set | frozenset) else sorted(map(str, v))
            for k, v in m.kwargs.items()
            if k.startswith(("allow_", "expose", "max"))
        }
        for m in app.user_middleware
        if m.cls.__name__ == "CORSMiddleware"
    ]

    assert [m.cls.__name__ for m in app.user_middleware] == SNAPSHOT[env_name]["user_middleware"]
    assert chain == SNAPSHOT[env_name]["chain"]
    assert cors == SNAPSHOT[env_name]["cors"]


def test_approvals_so_admin_em_todos_os_metodos_e_prefixos_parecidos_nao() -> None:
    for method in ("GET", "POST", "PUT", "PATCH", "DELETE"):
        assert classify_route(method, "/approvals", docs_require_admin=False).name == "ADMIN"
        assert classify_route(method, "/approvals/abc/resolve", docs_require_admin=False).name == "ADMIN"
        assert classify_route(method, "/approvals/", docs_require_admin=False).name == "ADMIN"
    # Só o segmento inteiro: um path vizinho não vira admin nem escapa por prefixo de string.
    assert classify_route("GET", "/approvalsx", docs_require_admin=False).name == "RUN"


APPROVALS_VARIANTS = [
    "/approvals", "/approvals/", "/approvals/x", "/approvals/x/resolve", "//approvals", "/approvals//x",
    "/x/../approvals", "/./approvals", "/approvals%2Fx", "/%61pprovals", "/approvals?x=1",
]


@pytest.mark.parametrize("path", APPROVALS_VARIANTS)
@pytest.mark.parametrize("method", ["GET", "POST", "DELETE"])
def test_approvals_com_a_chave_run_nunca_passa_da_auth_na_app_montada(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str
) -> None:
    from tests.fakes import loopback_client

    app = _mounted_app(monkeypatch, ENVIRONMENTS["prod"])
    client = loopback_client(app, raise_server_exceptions=False)
    run_auth = {"Authorization": "Bearer " + "r" * 40}

    response = client.request(method, f"http://127.0.0.1:7777{path}", headers=run_auth, follow_redirects=False)
    admin = client.request(method, "/approvals", headers={"Authorization": "Bearer " + "a" * 40})

    # 403 (auth) ou 404 (rota que não existe): nunca o stub 503 do AgentOS, que só o admin alcança.
    assert response.status_code in (403, 404, 307, 308), (method, path, response.status_code)
    assert admin.status_code == 503
