"""Classificador de rotas e política de chaves do F1-04 (``api_key_auth``).

A tabela abaixo é a de rotas REAIS do app com o AgentOS (agno 2.5.8) montado com um
Agent e um Team de modelo fake. Rota nova no agno (ou no app) quebra
``test_tabela_cobre_todas_as_rotas_do_app``: classifique-a aqui antes de seguir.
"""

from __future__ import annotations

import re
from collections.abc import Iterator

import pytest
from agno.agent import Agent
from agno.team import Team
from fastapi import FastAPI
from starlette.routing import Route, WebSocketRoute

from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.web.api_key_auth import (
    ApiKeys,
    RouteAccess,
    classify_route,
    is_local_dev_mode,
    resolve_api_keys,
)
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import FakeChatModel

RUN_KEY = "chave-de-teste-run-" + "r" * 21
ADMIN_KEY = "chave-de-teste-admin-" + "a" * 19

# MÉTODO PATH CLASSE. DOCS = admin fora de development, run em development.
ROUTE_TABLE = """
GET /livez PUBLIC
GET /docs DOCS
HEAD /docs DOCS
GET /docs/oauth2-redirect DOCS
HEAD /docs/oauth2-redirect DOCS
GET /redoc DOCS
HEAD /redoc DOCS
GET /openapi.json DOCS
HEAD /openapi.json DOCS
GET /admin/health ADMIN
POST /admin/refresh-cache ADMIN
GET /metrics/cache ADMIN
GET /metrics ADMIN
POST /metrics/refresh ADMIN
POST /databases/all/migrate ADMIN
POST /databases/{db_id}/migrate ADMIN
GET /eval-runs ADMIN
POST /eval-runs ADMIN
DELETE /eval-runs ADMIN
GET /eval-runs/{eval_run_id} ADMIN
PATCH /eval-runs/{eval_run_id} ADMIN
DELETE /sessions ADMIN
DELETE /sessions/{session_id} ADMIN
DELETE /memories ADMIN
DELETE /memories/{memory_id} ADMIN
POST /knowledge/content ADMIN
DELETE /knowledge/content ADMIN
POST /knowledge/remote-content ADMIN
PATCH /knowledge/content/{content_id} ADMIN
DELETE /knowledge/content/{content_id} ADMIN
POST /knowledge/search ADMIN
GET /components ADMIN
POST /components ADMIN
PUT /components ADMIN
PATCH /components ADMIN
DELETE /components ADMIN
GET /components/{path:path} ADMIN
POST /components/{path:path} ADMIN
PUT /components/{path:path} ADMIN
PATCH /components/{path:path} ADMIN
DELETE /components/{path:path} ADMIN
GET /schedules ADMIN
POST /schedules ADMIN
PUT /schedules ADMIN
PATCH /schedules ADMIN
DELETE /schedules ADMIN
GET /schedules/{path:path} ADMIN
POST /schedules/{path:path} ADMIN
PUT /schedules/{path:path} ADMIN
PATCH /schedules/{path:path} ADMIN
DELETE /schedules/{path:path} ADMIN
GET /registry ADMIN
POST /optimize-memories ADMIN
GET / RUN
GET /health RUN
GET /config RUN
GET /models RUN
GET /status RUN
POST /agui RUN
POST /agui/{entity_id} RUN
GET /agents RUN
GET /agents/{agent_id} RUN
POST /agents/{agent_id}/runs RUN
GET /agents/{agent_id}/runs RUN
GET /agents/{agent_id}/runs/{run_id} RUN
POST /agents/{agent_id}/runs/{run_id}/cancel RUN
POST /agents/{agent_id}/runs/{run_id}/continue RUN
GET /teams RUN
GET /teams/{team_id} RUN
POST /teams/{team_id}/runs RUN
GET /teams/{team_id}/runs RUN
GET /teams/{team_id}/runs/{run_id} RUN
POST /teams/{team_id}/runs/{run_id}/cancel RUN
GET /workflows RUN
GET /workflows/{workflow_id} RUN
POST /workflows/{workflow_id}/runs RUN
GET /workflows/{workflow_id}/runs/{run_id} RUN
POST /workflows/{workflow_id}/runs/{run_id}/cancel RUN
WS /workflows/ws RUN
GET /sessions RUN
POST /sessions RUN
GET /sessions/{session_id} RUN
PATCH /sessions/{session_id} RUN
POST /sessions/{session_id}/rename RUN
GET /sessions/{session_id}/runs RUN
GET /sessions/{session_id}/runs/{run_id} RUN
GET /memories RUN
POST /memories RUN
GET /memories/{memory_id} RUN
PATCH /memories/{memory_id} RUN
GET /memory_topics RUN
GET /user_memory_stats RUN
GET /knowledge/config RUN
GET /knowledge/content RUN
GET /knowledge/content/{content_id} RUN
GET /knowledge/content/{content_id}/status RUN
GET /knowledge/{knowledge_id}/sources RUN
GET /knowledge/{knowledge_id}/sources/{source_id}/files RUN
GET /traces RUN
GET /traces/filter-schema RUN
GET /traces/{trace_id} RUN
POST /traces/search RUN
GET /trace_session_stats RUN
GET /approvals RUN
POST /approvals RUN
PUT /approvals RUN
PATCH /approvals RUN
DELETE /approvals RUN
GET /approvals/{path:path} RUN
POST /approvals/{path:path} RUN
PUT /approvals/{path:path} RUN
PATCH /approvals/{path:path} RUN
DELETE /approvals/{path:path} RUN
"""


def _table() -> dict[tuple[str, str], str]:
    rows = [line.split() for line in ROUTE_TABLE.strip().splitlines()]
    return {(method, path): access for method, path, access in rows}


TABLE = _table()


def _sample(template: str) -> str:
    """Path concreto para um template (``{agent_id}`` -> ``x1``; ``{path:path}`` -> ``x1/x2``)."""
    return re.sub(r"\{[^}]+\}", lambda m: "x1/x2" if m.group(0).endswith(":path}") else "x1", template)


def _expected(access: str, *, docs_require_admin: bool) -> RouteAccess:
    if access == "DOCS":
        return RouteAccess.ADMIN if docs_require_admin else RouteAccess.RUN
    return RouteAccess[access]


@pytest.fixture(scope="module")
def mounted_app() -> Iterator[FastAPI]:
    with pytest.MonkeyPatch.context() as mp:
        for name in ("ENVIRONMENT", "API_KEY_RUN", "API_KEY_ADMIN", "APP_HOST"):
            mp.delenv(name, raising=False)
        mp.setenv("ENABLE_DOCS", "true")
        mp.setenv("AGNO_TELEMETRY", "false")
        agent = Agent(id="agente-1", name="Agente 1", model=FakeChatModel(responses=[]), telemetry=False)
        member = Agent(id="membro-1", name="Membro", model=FakeChatModel(responses=[]), telemetry=False)
        team = Team(id="time-1", name="Time 1", members=[member], model=FakeChatModel(responses=[]), telemetry=False)
        factory = AppFactory()
        app = factory.create_app()
        factory._mount_agent_os(app, [agent], [team])
        yield app


def _real_routes(app: FastAPI) -> set[tuple[str, str]]:
    routes: set[tuple[str, str]] = set()
    for route in app.routes:
        if isinstance(route, WebSocketRoute):
            routes.add(("WS", route.path))
        elif isinstance(route, Route):
            routes.update((method, route.path) for method in route.methods or ())
        else:
            # Mount/Host/rota customizada: um sub-app inteiro que a tabela não enxerga.
            pytest.fail(f"tipo de rota sem classificação: {type(route).__name__} {getattr(route, 'path', '')}")
    return routes


def test_tabela_cobre_todas_as_rotas_do_app(mounted_app: FastAPI):
    real = _real_routes(mounted_app)

    assert real - TABLE.keys() == set(), "rota sem classificação na tabela: classifique antes de seguir"
    assert TABLE.keys() - real == set(), "rota da tabela que o app não tem mais"


@pytest.mark.parametrize("docs_require_admin", [True, False], ids=["fora-de-development", "development"])
@pytest.mark.parametrize(("method", "template"), sorted(TABLE), ids=lambda v: str(v))
def test_toda_rota_real_cai_na_classe_esperada(method: str, template: str, docs_require_admin: bool):
    expected = _expected(TABLE[(method, template)], docs_require_admin=docs_require_admin)
    http_method = "GET" if method == "WS" else method

    assert classify_route(http_method, _sample(template), docs_require_admin=docs_require_admin) is expected


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/inventada"),
        ("POST", "/qualquer/coisa"),
        ("DELETE", "/agents/x1"),
        ("GET", "/livez/extra"),
        ("GET", "/adminx"),
        ("GET", "/metricsx"),
        ("DELETE", "/sessionsx"),
        ("POST", "/optimize-memoriesx"),
        ("OPTIONS", "/agents"),
    ],
)
def test_rota_desconhecida_exige_run(method: str, path: str):
    assert classify_route(method, path, docs_require_admin=True) is RouteAccess.RUN


@pytest.mark.parametrize("method", ["get", "Post", "delete"])
def test_metodo_e_normalizado(method: str):
    assert classify_route(method, "/admin/health", docs_require_admin=True) is RouteAccess.ADMIN
    expected = RouteAccess.ADMIN if method.upper() == "DELETE" else RouteAccess.RUN
    assert classify_route(method, "/sessions/s1", docs_require_admin=True) is expected


# ── política de chaves (fail-closed) ────────────────────────────────


def _config(*, host: str, environment: str, keys: bool) -> AppConfig:
    return AppConfig(
        mongo_connection_string="mongodb://x",
        mongo_database_name="db",
        app_title="t",
        app_host=host,
        app_port=7777,
        log_level="INFO",
        ollama_base_url=None,
        environment=environment,
        api_key_run=RUN_KEY if keys else None,
        api_key_admin=ADMIN_KEY if keys else None,
    )


@pytest.mark.parametrize(
    "host", ["127.0.0.1", "127.0.0.2", "127.255.255.254", "::1", "[::1]", "localhost", "LOCALHOST"]
)
@pytest.mark.parametrize("environment", ["development", "test"])
def test_sem_chaves_em_loopback_e_dev_ou_test_libera(host: str, environment: str):
    assert resolve_api_keys(_config(host=host, environment=environment, keys=False)) is None


@pytest.mark.parametrize(
    ("host", "environment"),
    [
        ("0.0.0.0", "development"),  # noqa: S104 - é exatamente o bind recusado
        ("::", "development"),
        ("192.168.0.10", "test"),
        ("app.example.com", "development"),
        ("::ffff:127.0.0.1", "development"),
        ("127.0.0.1", "production"),
        ("127.0.0.1", "staging"),
        ("localhost", "production"),
    ],
)
def test_sem_chaves_fora_do_modo_dev_local_recusa(host: str, environment: str):
    with pytest.raises(ValueError, match=r"API_KEY_RUN e API_KEY_ADMIN.*secrets\.token_urlsafe") as exc:
        resolve_api_keys(_config(host=host, environment=environment, keys=False))
    assert host in str(exc.value) and environment in str(exc.value)


@pytest.mark.parametrize(
    ("host", "environment", "keys", "expected"),
    [
        ("127.0.0.1", "development", False, True),
        ("localhost", "test", False, True),
        ("127.0.0.1", "development", True, False),  # com chaves não é modo dev local
        ("0.0.0.0", "development", False, False),  # noqa: S104
        ("127.0.0.1", "production", False, False),
        ("::ffff:127.0.0.1", "development", False, False),
    ],
)
def test_is_local_dev_mode_e_a_mesma_regra_da_borda(host: str, environment: str, keys: bool, expected: bool):
    """F2-02: o registry de providers reutiliza esta regra (base_url em loopback só no modo dev local)."""
    assert is_local_dev_mode(_config(host=host, environment=environment, keys=keys)) is expected


@pytest.mark.parametrize(("host", "environment"), [("0.0.0.0", "production"), ("127.0.0.1", "development")])  # noqa: S104
def test_com_chaves_vale_em_qualquer_bind(host: str, environment: str):
    keys = resolve_api_keys(_config(host=host, environment=environment, keys=True))

    assert keys == ApiKeys(run=RUN_KEY.encode(), admin=ADMIN_KEY.encode())
    assert RUN_KEY not in repr(keys) and ADMIN_KEY not in repr(keys)


def test_real_routes_falha_em_tipo_de_rota_desconhecido():
    """Um ``Mount`` (ex.: MCP do AgentOS em ``/``) esconderia rotas da tabela: falha alto."""
    from starlette.applications import Starlette
    from starlette.routing import Mount

    app = FastAPI()
    app.router.routes.append(Mount("/sub", app=Starlette()))

    with pytest.raises(pytest.fail.Exception, match="Mount /sub"):
        _real_routes(app)
