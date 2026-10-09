"""Higiene de borda (F1-03): health sem vazamento, docs desligáveis, CORS por env, /livez.

Tudo com a pilha real do ``AppFactory`` e, onde importa, o AgentOS montado com um
``Agent`` de modelo fake (sem Mongo, sem rede).
"""

from __future__ import annotations

from collections.abc import Callable

import pytest
from agno.agent import Agent
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.infrastructure.dependency_injection import HealthService
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import FakeChatModel, RecordingLogger, loopback_client

# Texto que só existe na exceção do driver: não pode chegar ao cliente HTTP.
SEGREDO_NA_EXCECAO = "mongodb://admin:senha-super-secreta@mongo.interno:27017"
DOC_ROUTES = ("/docs", "/redoc", "/openapi.json")


class _UnreachableAdmin:
    async def command(self, name: str) -> dict[str, int]:
        raise ConnectionError(f"falha ao conectar em {SEGREDO_NA_EXCECAO} ({name})")


class _MongoClientDown:
    """Cliente Mongo cujo ping sempre falha (servidor fora do ar)."""

    admin = _UnreachableAdmin()


class _HealthOnlyContainer:
    """Container mínimo: o endpoint de health só usa ``health_service``."""

    def __init__(self, health_service: HealthService) -> None:
        self.health_service = health_service

    async def cleanup(self) -> None:
        return None


def _agent() -> Agent:
    return Agent(id="agente-1", name="Agente 1", model=FakeChatModel(responses=[]), telemetry=False)


@pytest.fixture
def build_app(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FastAPI]:
    """Cria o app com as variáveis pedidas; ``with_agent_os`` monta o AgentOS como o lifespan faz."""

    def _build(*, with_agent_os: bool = False, **env: str) -> FastAPI:
        for name in ("ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS"):
            monkeypatch.delenv(name, raising=False)
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        factory = AppFactory()
        app = factory.create_app()
        if with_agent_os:
            factory._mount_agent_os(app, [_agent()], [])
        return app

    return _build


# ── /admin/health ───────────────────────────────────────────────────


def test_health_com_mongo_fora_responde_503_sem_texto_de_excecao():
    logger = RecordingLogger()
    factory = AppFactory()
    app = factory.create_app()
    factory._container = _HealthOnlyContainer(HealthService(_MongoClientDown(), logger))  # type: ignore[assignment]

    response = loopback_client(app).get("/admin/health")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "unhealthy"
    assert body["checks"]["mongodb"] == {"status": "unhealthy"}
    raw = response.text
    for leaked in ("senha-super-secreta", "mongo.interno", "falha ao conectar", "ConnectionError", "Traceback"):
        assert leaked not in raw, leaked
    # O detalhe fica só no log interno, pelo tipo da exceção (sem a mensagem).
    errors = [r for r in logger.records if r.level in ("warning", "error")]
    assert [r.context.get("error_type") for r in errors] == ["ConnectionError"]
    assert all("senha-super-secreta" not in str(r.context) for r in logger.records)


# ── /livez ──────────────────────────────────────────────────────────


def test_livez_responde_ok_sem_dependencias(build_app: Callable[..., FastAPI]):
    """Sem container (Mongo nunca tocado): o processo está vivo e é só isso que ele diz."""
    response = loopback_client(build_app()).get("/livez")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


# ── docs ────────────────────────────────────────────────────────────


@pytest.mark.parametrize("with_agent_os", [False, True], ids=["so-base-app", "com-agentos"])
def test_docs_somem_com_enable_docs_false(build_app: Callable[..., FastAPI], with_agent_os: bool):
    client = loopback_client(build_app(with_agent_os=with_agent_os, ENABLE_DOCS="false"))

    assert {path: client.get(path).status_code for path in DOC_ROUTES} == dict.fromkeys(DOC_ROUTES, 404)


def test_docs_desligados_por_padrao_fora_de_development(build_app: Callable[..., FastAPI]):
    # Fora de development o app exige chaves (F1-04) e docs são rota admin. Valores de teste.
    keys = {"API_KEY_RUN": "chave-de-teste-run-" + "r" * 21, "API_KEY_ADMIN": "chave-de-teste-admin-" + "a" * 19}
    app = build_app(with_agent_os=True, ENVIRONMENT="production", **keys)
    client = loopback_client(app, headers={"Authorization": f"Bearer {keys['API_KEY_ADMIN']}"})

    assert {path: client.get(path).status_code for path in DOC_ROUTES} == dict.fromkeys(DOC_ROUTES, 404)


def test_docs_continuam_disponiveis_quando_habilitados(build_app: Callable[..., FastAPI]):
    """Controle positivo: sem isto, o 404 acima poderia vir de qualquer outra coisa."""
    client = loopback_client(build_app(with_agent_os=True, ENABLE_DOCS="true"))

    assert {path: client.get(path).status_code for path in DOC_ROUTES} == dict.fromkeys(DOC_ROUTES, 200)
    assert "/agents/{agent_id}/runs" in client.get("/openapi.json").json()["paths"]


# ── CORS ────────────────────────────────────────────────────────────


def _preflight(client: TestClient, origin: str, *, method: str = "POST", headers: str = "") -> object:
    request_headers = {"Origin": origin, "Access-Control-Request-Method": method}
    if headers:
        request_headers["Access-Control-Request-Headers"] = headers
    return client.options("/agents/agente-1/runs", headers=request_headers)


@pytest.mark.parametrize("with_agent_os", [False, True], ids=["so-base-app", "com-agentos"])
def test_cors_respeita_a_env(build_app: Callable[..., FastAPI], with_agent_os: bool):
    permitida = "https://painel.example.com"
    app = build_app(with_agent_os=with_agent_os, CORS_ALLOWED_ORIGINS=f" {permitida} , https://outra.example.com")
    client = loopback_client(app)

    ok = _preflight(client, permitida, headers="Authorization, Content-Type, X-API-Key")
    assert ok.status_code == 200
    assert ok.headers["access-control-allow-origin"] == permitida

    # Origens do default (e as que o AgentOS acrescentaria) deixam de valer.
    for negada in ("http://localhost:3000", "https://os-stg.agno.com", "https://evil.example.com"):
        denied = _preflight(client, negada)
        assert denied.status_code == 400, negada
        assert "access-control-allow-origin" not in denied.headers, negada

    simple = client.get("/livez", headers={"Origin": "https://evil.example.com"})
    assert "access-control-allow-origin" not in simple.headers


@pytest.mark.parametrize("with_agent_os", [False, True], ids=["so-base-app", "com-agentos"])
def test_cors_metodos_e_headers_explicitos(build_app: Callable[..., FastAPI], with_agent_os: bool):
    client = loopback_client(build_app(with_agent_os=with_agent_os))
    origin = "https://os.agno.com"  # está no default

    ok = _preflight(client, origin, method="PATCH", headers="authorization,content-type,x-api-key")
    assert ok.status_code == 200
    allowed_methods = {m.strip() for m in ok.headers["access-control-allow-methods"].split(",")}
    assert allowed_methods == {"GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}

    assert _preflight(client, origin, headers="X-Header-Arbitrario").status_code == 400
    assert _preflight(client, origin, method="TRACE").status_code == 400


# ── posição e forma do CORS ─────────────────────────────────────────


def _assert_cors_outermost_and_explicit(app: FastAPI, origins: list[str]) -> None:
    from fastapi.middleware.cors import CORSMiddleware

    assert app.user_middleware[0].cls is CORSMiddleware, "CORS precisa ser o middleware mais externo"
    assert [m.cls for m in app.user_middleware].count(CORSMiddleware) == 1
    kwargs = app.user_middleware[0].kwargs
    assert kwargs["allow_origins"] == origins
    assert "*" not in kwargs["allow_methods"] and "*" not in kwargs["allow_headers"]


@pytest.mark.parametrize("with_agent_os", [False, True], ids=["so-base-app", "com-agentos"])
def test_cors_e_o_middleware_mais_externo(build_app: Callable[..., FastAPI], with_agent_os: bool):
    app = build_app(with_agent_os=with_agent_os, CORS_ALLOWED_ORIGINS="https://painel.example.com")

    _assert_cors_outermost_and_explicit(app, ["https://painel.example.com"])


def test_cors_reaplicado_mesmo_se_a_montagem_do_agentos_falhar(monkeypatch: pytest.MonkeyPatch):
    """O AgentOS troca o CORS (``*``) antes de terminar o get_app(); falha no meio não pode deixá-lo assim."""
    from agno.os.utils import update_cors_middleware

    from src.infrastructure.web import app_factory

    class _AgentOSQueQuebra:
        def __init__(self, *, base_app: FastAPI, **_: object) -> None:
            self._app = base_app

        def get_app(self) -> FastAPI:
            update_cors_middleware(self._app, ["https://evil.example.com"])
            raise RuntimeError("falha no meio da montagem")

    monkeypatch.setenv("CORS_ALLOWED_ORIGINS", "https://painel.example.com")
    monkeypatch.setattr(app_factory, "AgentOS", _AgentOSQueQuebra)
    factory = AppFactory()
    app = factory.create_app()

    with pytest.raises(RuntimeError, match="falha no meio"):
        factory._mount_agent_os(app, [_agent()], [])

    _assert_cors_outermost_and_explicit(app, ["https://painel.example.com"])
    denied = _preflight(loopback_client(app), "https://evil.example.com")
    assert denied.status_code == 400


def test_cors_expoe_so_o_sinal_de_deprecated_do_agui(build_app: Callable[..., FastAPI]):
    """F1-08: só ``Deprecation`` e ``Link`` (alias ``POST /agui``) ficam legíveis no navegador."""
    response = loopback_client(build_app()).get("/livez", headers={"Origin": "https://os.agno.com"})

    assert response.headers["access-control-allow-origin"] == "https://os.agno.com"
    assert response.headers["access-control-expose-headers"] == "Deprecation, Link"
