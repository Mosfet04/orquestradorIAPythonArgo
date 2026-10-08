"""Auth fail-closed na borda (F1-04): chaves run/admin em HTTP e WebSocket.

Pilha real do ``AppFactory`` com o AgentOS montado com um Agent de modelo fake (sem
Mongo, sem rede). As chaves daqui são valores de teste, não segredos.
"""

from __future__ import annotations

import logging
import os
import shutil
import subprocess
import sys
from collections.abc import Callable, Iterator
from pathlib import Path
from typing import Any

import pytest
from agno.agent import Agent
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from starlette.testclient import TestClient
from starlette.types import Message, Receive, Scope, Send
from starlette.websockets import WebSocketDisconnect

from src.infrastructure.web.api_key_auth import ApiKeyAuthMiddleware, ApiKeys
from src.infrastructure.web.app_factory import AppFactory
from src.infrastructure.web.metrics_middleware import MetricsMiddleware
from tests.fakes import FakeChatModel, RecordingLogger, loopback_client

REPO = Path(__file__).resolve().parents[2]
RUN_KEY = "chave-de-teste-run-" + "r" * 21
ADMIN_KEY = "chave-de-teste-admin-" + "a" * 19
# Mesmo tamanho da chave run, último caractere diferente.
WRONG_KEY = RUN_KEY[:-1] + "x"
ALLOWED = "https://painel.example.com"
KEYS = {"API_KEY_RUN": RUN_KEY, "API_KEY_ADMIN": ADMIN_KEY}
AGUI_BODY = {
    "threadId": "t1",
    "runId": "r1",
    "state": {},
    "messages": [{"id": "m1", "role": "user", "content": "oi"}],
    "tools": [],
    "context": [],
    "forwardedProps": {},
}
UNAUTHORIZED = {"detail": "unauthorized"}
FORBIDDEN = {"detail": "forbidden"}

_ENV_NAMES = ("ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS", "APP_HOST", "API_KEY_RUN", "API_KEY_ADMIN")


def _agent() -> Agent:
    return Agent(
        id="agente-1",
        name="Agente 1",
        model=FakeChatModel(responses=["resposta"] * 10),
        telemetry=False,
    )


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _make_app(mp: pytest.MonkeyPatch, *, agent: Agent | None, **env: str) -> FastAPI:
    """App com as variáveis pedidas (ambiente limpo); com ``agent`` monta o AgentOS como o lifespan."""
    for name in _ENV_NAMES:
        mp.delenv(name, raising=False)
    mp.setenv("AGNO_TELEMETRY", "false")
    mp.setenv("CORS_ALLOWED_ORIGINS", ALLOWED)
    for name, value in env.items():
        mp.setenv(name, value)
    factory = AppFactory()
    app = factory.create_app()
    if agent is not None:
        factory._mount_agent_os(app, [agent], [])
    return app


@pytest.fixture
def build_app(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FastAPI]:
    """App com as variáveis pedidas (ambiente limpo); ``with_agent_os`` monta como o lifespan."""

    def _build(*, with_agent_os: bool = True, **env: str) -> FastAPI:
        return _make_app(monkeypatch, agent=_agent() if with_agent_os else None, **env)

    return _build


# Produção típica: bind em todas as interfaces, chaves configuradas.
PROD_ENV = {"APP_HOST": "0.0.0.0", "ENVIRONMENT": "production", **KEYS}  # noqa: S104


@pytest.fixture
def client(build_app: Callable[..., FastAPI]) -> TestClient:
    """App de produção novo a cada teste (para quem passa da auth e executa handler)."""
    return TestClient(build_app(**PROD_ENV))


@pytest.fixture(scope="module")
def shared_prod_app() -> Iterator[FastAPI]:
    """O mesmo app de ``client``, montado uma vez por módulo (e por worker do xdist).

    Só para testes cujo request não executa handler com estado: a auth recusa (401/403/1008),
    o CORS responde (preflight), o roteador não casa (404) ou a rota é pública e sem estado
    (``/livez``). Auth e CORS são imutáveis depois do ``create_app`` (as chaves são lidas só
    ali) e o env é restaurado logo após a montagem. A guarda do teardown prova que nenhum
    teste deste app chegou ao modelo.
    """
    agent = _agent()
    with pytest.MonkeyPatch.context() as mp:
        app = _make_app(mp, agent=agent, **PROD_ENV)
    yield app
    assert agent.model.calls == [], "teste com o app compartilhado executou o modelo: use a fixture `client`"  # type: ignore[union-attr]


@pytest.fixture
def rejecting_client(shared_prod_app: FastAPI) -> TestClient:
    """Cliente novo (sem cookies herdados) sobre ``shared_prod_app``; ver as restrições de lá."""
    return TestClient(shared_prod_app)


def _assert_unauthorized(response: object) -> None:
    assert response.status_code == 401  # type: ignore[attr-defined]
    assert response.json() == UNAUTHORIZED  # type: ignore[attr-defined]
    assert response.headers["www-authenticate"] == "Bearer"  # type: ignore[attr-defined]


# ── 401 sem credencial ──────────────────────────────────────────────

NO_TOKEN_REQUESTS = [
    ("POST", "/agents/agente-1/runs"),
    ("POST", "/agui"),
    ("GET", "/agents"),
    ("GET", "/config"),
    ("GET", "/health"),
    ("GET", "/sessions"),
    ("GET", "/admin/health"),
    ("POST", "/admin/refresh-cache"),
    ("GET", "/metrics/cache"),
    ("GET", "/rota-inventada"),
    ("GET", "/docs"),
]


@pytest.mark.parametrize(("method", "path"), NO_TOKEN_REQUESTS)
def test_sem_credencial_responde_401_generico(rejecting_client: TestClient, method: str, path: str):
    _assert_unauthorized(rejecting_client.request(method, path))


def test_rota_desconhecida_sem_credencial_e_401_e_nao_404(rejecting_client: TestClient):
    """Sem chave não dá para enumerar rotas: 401 antes do roteamento."""
    _assert_unauthorized(rejecting_client.get("/nao-existe"))
    assert rejecting_client.get("/nao-existe", headers=_bearer(RUN_KEY)).status_code == 404


@pytest.mark.parametrize(
    "headers",
    [
        _bearer(WRONG_KEY),
        {"X-API-Key": WRONG_KEY},
        _bearer(RUN_KEY[:-1]),
        _bearer(RUN_KEY + "x"),
        _bearer(""),
        {"Authorization": "Bearer"},
        {"Authorization": RUN_KEY},
        {"Authorization": f"Basic {RUN_KEY}"},
        {"Authorization": f"Token {RUN_KEY}"},
        {"X-API-Key": ""},
        {"X-Api-Key-Other": RUN_KEY},
    ],
    ids=[
        "bearer-errada-mesmo-tamanho",
        "x-api-key-errada-mesmo-tamanho",
        "prefixo-da-chave",
        "chave-mais-um-caractere",
        "bearer-vazio",
        "bearer-sem-token",
        "sem-esquema",
        "basic",
        "outro-esquema",
        "x-api-key-vazia",
        "header-errado",
    ],
)
def test_credencial_invalida_responde_401(rejecting_client: TestClient, headers: dict[str, str]):
    form = {"message": "oi", "stream": "false"}
    _assert_unauthorized(rejecting_client.post("/agents/agente-1/runs", data=form, headers=headers))


# ── 403 com chave run em rota admin ─────────────────────────────────

ADMIN_REQUESTS = [
    ("GET", "/admin/health"),
    ("GET", "/admin/health/"),
    ("POST", "/admin/refresh-cache"),
    ("GET", "/metrics/cache"),
    ("GET", "/metrics"),
    ("POST", "/metrics/refresh"),
    ("POST", "/databases/all/migrate"),
    ("POST", "/databases/db-1/migrate"),
    ("DELETE", "/sessions"),
    ("DELETE", "/sessions/s1"),
    ("DELETE", "/memories"),
    ("DELETE", "/memories/m1"),
    ("POST", "/knowledge/content"),
    ("DELETE", "/knowledge/content/c1"),
    ("GET", "/eval-runs"),
    ("POST", "/eval-runs"),
    ("GET", "/docs"),
    ("GET", "/openapi.json"),
]


@pytest.mark.parametrize(("method", "path"), ADMIN_REQUESTS)
def test_chave_run_em_rota_admin_responde_403(rejecting_client: TestClient, method: str, path: str):
    response = rejecting_client.request(method, path, headers=_bearer(RUN_KEY))

    assert response.status_code == 403
    assert response.json() == FORBIDDEN


def test_root_path_nao_esconde_rota_admin(build_app: Callable[..., FastAPI]):
    """Com root_path (proxy), a classificação usa o path de rota, como o roteador do Starlette.

    O uvicorn com ``--root-path`` entrega ``path`` já prefixado; o TestClient não prefixa,
    então o request leva o prefixo explícito.
    """
    app = build_app(ENVIRONMENT="production", APP_HOST="0.0.0.0", **KEYS)  # noqa: S104
    client = TestClient(app, root_path="/api")

    assert client.get("/api/admin/health", headers=_bearer(RUN_KEY)).status_code == 403
    assert client.get("/api/admin/health", headers=_bearer(ADMIN_KEY)).status_code == 200
    assert client.get("/api/livez").status_code == 200


# ── acesso concedido ────────────────────────────────────────────────


@pytest.mark.parametrize("key", [RUN_KEY, ADMIN_KEY], ids=["run", "admin"])
@pytest.mark.parametrize("style", ["bearer", "bearer-minusculo", "x-api-key"])
def test_chave_valida_libera_rotas_de_run(client: TestClient, key: str, style: str):
    headers = {
        "bearer": {"Authorization": f"Bearer {key}"},
        "bearer-minusculo": {"Authorization": f"bearer {key}"},
        "x-api-key": {"X-API-Key": key},
    }[style]

    run = client.post("/agents/agente-1/runs", data={"message": "oi", "stream": "false"}, headers=headers)
    agui = client.post("/agui", json=AGUI_BODY, headers=headers)
    agents = client.get("/agents", headers=headers)

    assert run.status_code == 200 and run.json()["content"] == "resposta"
    assert agui.status_code == 200 and "RUN_FINISHED" in agui.text
    assert agents.status_code == 200 and [a["id"] for a in agents.json()] == ["agente-1"]


def test_chave_admin_libera_rotas_admin(client: TestClient):
    health = client.get("/admin/health", headers=_bearer(ADMIN_KEY))
    cache = client.get("/metrics/cache", headers={"X-API-Key": ADMIN_KEY})

    assert health.status_code == 200 and health.json() == {"status": "healthy"}
    assert cache.status_code == 200 and cache.json() == {"status": "no_cache"}


def test_bearer_tem_precedencia_sobre_x_api_key(client: TestClient):
    """Com os dois headers vale o Authorization; o X-API-Key só é lido sem Bearer."""
    both_run_bearer = {"Authorization": f"Bearer {RUN_KEY}", "X-API-Key": ADMIN_KEY}
    basic_and_key = {"Authorization": "Basic dXNlcjpwdw==", "X-API-Key": ADMIN_KEY}

    assert client.get("/admin/health", headers=both_run_bearer).status_code == 403
    assert client.get("/admin/health", headers=basic_and_key).status_code == 200


@pytest.mark.parametrize("path", ["/livez", "/livez/"])
def test_livez_e_publico(rejecting_client: TestClient, path: str):
    """``/livez/`` também: o TrailingSlashMiddleware do AgentOS leva o request à mesma rota."""
    response = rejecting_client.get(path)

    assert response.status_code == 200 and response.json() == {"status": "ok"}


# ── CORS: preflight e 401 com headers ───────────────────────────────


@pytest.mark.parametrize("path", ["/agents/agente-1/runs", "/agui", "/admin/health"])
def test_preflight_cors_passa_sem_token(rejecting_client: TestClient, path: str):
    response = rejecting_client.options(path, headers={"Origin": ALLOWED, "Access-Control-Request-Method": "POST"})

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == ALLOWED


def test_401_de_origem_permitida_leva_headers_cors(rejecting_client: TestClient):
    """Auth dentro do CORS: o navegador consegue ler o 401 (e pedir a chave)."""
    response = rejecting_client.post("/agui", json=AGUI_BODY, headers={"Origin": ALLOWED})

    _assert_unauthorized(response)
    assert response.headers["access-control-allow-origin"] == ALLOWED
    assert response.headers["access-control-allow-credentials"] == "true"


def test_options_sem_cabecalhos_de_preflight_exige_chave(rejecting_client: TestClient):
    """Só o preflight de verdade (Origin + Access-Control-Request-Method) é público."""
    _assert_unauthorized(rejecting_client.options("/agents/agente-1/runs"))
    _assert_unauthorized(rejecting_client.options("/agents/agente-1/runs", headers={"Origin": ALLOWED}))


# ── WebSocket ───────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "headers",
    [{}, _bearer(WRONG_KEY), {"X-API-Key": WRONG_KEY}],
    ids=["sem-chave", "bearer-errada", "x-api-key-errada"],
)
def test_websocket_sem_credencial_valida_fecha_com_1008_antes_de_aceitar(
    rejecting_client: TestClient, headers: dict[str, str]
):
    with pytest.raises(WebSocketDisconnect) as exc:
        with rejecting_client.websocket_connect("/workflows/ws", headers=headers):
            pass  # pragma: no cover - a conexão nunca é aceita

    assert exc.value.code == 1008
    assert exc.value.reason == ""


@pytest.mark.parametrize("headers", [_bearer(RUN_KEY), {"X-API-Key": ADMIN_KEY}], ids=["run-bearer", "admin-x-api-key"])
def test_websocket_com_chave_conecta(client: TestClient, headers: dict[str, str]):
    with client.websocket_connect("/workflows/ws", headers=headers) as ws:
        assert ws.receive_json()["event"] == "connected"


# ── ordem da pilha ──────────────────────────────────────────────────


@pytest.mark.parametrize("with_agent_os", [False, True], ids=["so-base-app", "com-agentos"])
@pytest.mark.parametrize("env", [KEYS, {}], ids=["com-chaves", "modo-dev-local"])
def test_auth_fica_logo_dentro_do_cors(build_app: Callable[..., FastAPI], with_agent_os: bool, env: dict[str, str]):
    app = build_app(with_agent_os=with_agent_os, **env)
    classes = [m.cls for m in app.user_middleware]

    assert classes[:2] == [CORSMiddleware, ApiKeyAuthMiddleware]
    assert classes.count(ApiKeyAuthMiddleware) == 1


@pytest.mark.parametrize("with_agent_os", [False, True], ids=["so-base-app", "com-agentos"])
@pytest.mark.parametrize("env", [KEYS, {}], ids=["com-chaves", "modo-dev-local"])
def test_nada_que_reescreva_path_fica_dentro_da_auth(
    build_app: Callable[..., FastAPI], with_agent_os: bool, env: dict[str, str]
):
    """Middleware dentro da auth vê o request depois da classificação: se reescrevesse o
    path (ex.: tirar ``/playground``), uma rota admin passaria com a chave run."""
    from agno.os.middleware.trailing_slash import TrailingSlashMiddleware

    classes = [m.cls for m in build_app(with_agent_os=with_agent_os, **env).user_middleware]
    inner = classes[classes.index(ApiKeyAuthMiddleware) + 1 :]

    assert set(inner) <= {TrailingSlashMiddleware, MetricsMiddleware}, inner


def test_auth_sobrevive_a_falha_na_montagem_do_agentos(monkeypatch: pytest.MonkeyPatch):
    from agno.os.utils import update_cors_middleware

    from src.infrastructure.web import app_factory

    class _AgentOSQueQuebra:
        def __init__(self, *, base_app: FastAPI, **_: object) -> None:
            self._app = base_app

        def get_app(self) -> FastAPI:
            update_cors_middleware(self._app, ["https://evil.example.com"])
            self._app.user_middleware = [m for m in self._app.user_middleware if m.cls is not ApiKeyAuthMiddleware]
            raise RuntimeError("falha no meio da montagem")

    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    for name, value in KEYS.items():
        monkeypatch.setenv(name, value)
    monkeypatch.setattr(app_factory, "AgentOS", _AgentOSQueQuebra)
    factory = AppFactory()
    app = factory.create_app()

    with pytest.raises(RuntimeError, match="falha no meio"):
        factory._mount_agent_os(app, [_agent()], [])

    assert [m.cls for m in app.user_middleware][:2] == [CORSMiddleware, ApiKeyAuthMiddleware]
    _assert_unauthorized(TestClient(app).get("/admin/health"))


# ── fail-closed no startup ──────────────────────────────────────────


@pytest.mark.parametrize(
    "env",
    [
        {"APP_HOST": "0.0.0.0"},  # noqa: S104 - bind recusado sem chaves
        {"APP_HOST": "0.0.0.0", "ENVIRONMENT": "development"},  # noqa: S104
        {"APP_HOST": "::"},
        {"APP_HOST": "127.0.0.1", "ENVIRONMENT": "production"},
        {"APP_HOST": "127.0.0.1", "ENVIRONMENT": "staging"},
        {"ENVIRONMENT": "production"},
    ],
)
def test_sem_chaves_fora_do_modo_dev_local_create_app_recusa(build_app: Callable[..., FastAPI], env: dict[str, str]):
    with pytest.raises(ValueError, match="API_KEY_RUN e API_KEY_ADMIN"):
        build_app(with_agent_os=False, **env)


@pytest.mark.parametrize("env", [{"API_KEY_RUN": RUN_KEY}, {"API_KEY_ADMIN": ADMIN_KEY}], ids=["so-run", "so-admin"])
def test_so_uma_chave_recusa_mesmo_no_modo_dev_local(build_app: Callable[..., FastAPI], env: dict[str, str]):
    with pytest.raises(ValueError, match="ausente"):
        build_app(with_agent_os=False, **env)


def _run_app_py(tmp_path: Path, **env: str) -> subprocess.CompletedProcess[str]:
    """Executa uma cópia de app.py num cwd temporário, sem .env e com ambiente mínimo."""
    shutil.copy(REPO / "app.py", tmp_path / "app.py")
    full_env = {"PATH": os.environ.get("PATH", ""), "PYTHONPATH": str(REPO), "PYTHONDONTWRITEBYTECODE": "1"}
    full_env.update(env)
    return subprocess.run(
        [sys.executable, "app.py"],
        cwd=tmp_path,
        env=full_env,
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )


def test_python_app_py_em_0000_sem_chaves_nao_chega_ao_bind(tmp_path: Path):
    result = _run_app_py(tmp_path, APP_HOST="0.0.0.0", ENVIRONMENT="production")  # noqa: S104

    assert result.returncode != 0
    assert "API_KEY_RUN e API_KEY_ADMIN" in result.stderr
    assert "Uvicorn running" not in result.stderr + result.stdout


# ── modo dev local ──────────────────────────────────────────────────


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
@pytest.mark.parametrize("environment", ["development", "test"])
def test_modo_dev_local_sem_chaves_libera_tudo_com_aviso(
    monkeypatch: pytest.MonkeyPatch, host: str, environment: str
):
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("APP_HOST", host)
    monkeypatch.setenv("ENVIRONMENT", environment)
    factory = AppFactory()
    logger = RecordingLogger()
    factory._logger = logger  # type: ignore[assignment]
    app = factory.create_app()
    factory._mount_agent_os(app, [_agent()], [])
    client = loopback_client(app)

    assert client.get("/admin/health").status_code == 200
    assert client.get("/agents").status_code == 200
    warnings = [r for r in logger.records if r.level == "warning"]
    assert len(warnings) == 1
    assert "Autenticação desligada" in warnings[0].message
    assert warnings[0].context == {"app_host": host, "environment": environment}


@pytest.fixture
def dev_app(build_app: Callable[..., FastAPI]) -> FastAPI:
    """Modo dev local: sem chaves, APP_HOST loopback, development."""
    return build_app(APP_HOST="127.0.0.1", ENVIRONMENT="development")


@pytest.mark.parametrize(("method", "path"), [("POST", "/admin/refresh-cache"), ("GET", "/agents"), ("GET", "/livez")])
def test_modo_dev_local_recusa_cliente_fora_do_loopback(dev_app: FastAPI, method: str, path: str):
    """``uvicorn --host 0.0.0.0`` direto, ou um túnel: o request chega de fora do loopback."""
    client = loopback_client(dev_app, client=("203.0.113.9", 40000))

    response = client.request(method, path)

    if path == "/livez":
        assert response.status_code == 200  # público em qualquer modo
    else:
        _assert_unauthorized(response)


def test_modo_dev_local_recusa_servidor_fora_do_loopback(dev_app: FastAPI):
    """Proxy no loopback com X-Forwarded-For forjado só reescreve o ``client``; o ``server``
    é o endereço local real do socket (aqui, uma interface de rede)."""
    client = loopback_client(dev_app, base_url="http://192.168.0.5:7777", headers={"Host": "localhost:7777"})

    _assert_unauthorized(client.post("/admin/refresh-cache"))


@pytest.mark.parametrize(
    "host", ["evil.example.com", "evil.example.com:7777", "127.0.0.1.nip.io", "localhost.evil.com"]
)
def test_modo_dev_local_recusa_host_fora_do_loopback(dev_app: FastAPI, host: str):
    """DNS rebinding: navegador fala com 127.0.0.1, mas o Host é o domínio do atacante."""
    client = loopback_client(dev_app, headers={"Host": host})

    _assert_unauthorized(client.get("/agents"))


def test_modo_dev_local_libera_acesso_local(dev_app: FastAPI):
    client = loopback_client(dev_app)

    assert client.post("/admin/refresh-cache").json() == {"status": "no_cache"}
    assert [a["id"] for a in client.get("/agents").json()] == ["agente-1"]


def test_modo_dev_local_websocket_so_do_loopback(dev_app: FastAPI):
    # websocket_connect ignora o base_url (usa ws://testserver): URL absoluta.
    url = "ws://127.0.0.1:7777/workflows/ws"
    with pytest.raises(WebSocketDisconnect) as exc:
        with loopback_client(dev_app, client=("203.0.113.9", 40000)).websocket_connect(url):
            pass  # pragma: no cover - a conexão nunca é aceita
    assert exc.value.code == 1008

    with loopback_client(dev_app).websocket_connect(url) as ws:
        assert ws.receive_json()["event"] == "connected"


# ── modo dev local: request cross-site de navegador (BUG-F1-04-DEV-CSRF) ──


@pytest.mark.parametrize(
    ("method", "path"), [("POST", "/admin/refresh-cache"), ("POST", "/databases/all/migrate"), ("GET", "/agents")]
)
def test_modo_dev_local_recusa_origem_nao_permitida(dev_app: FastAPI, method: str, path: str):
    """Página qualquer no navegador do dev manda POST simples (sem preflight) para 127.0.0.1."""
    client = loopback_client(dev_app)

    cross_site = {"Origin": "https://evil.example.com", "Sec-Fetch-Site": "cross-site"}
    response = client.request(method, path, headers=cross_site)

    _assert_unauthorized(response)


def test_modo_dev_local_aceita_origem_permitida_mesmo_cross_site(dev_app: FastAPI):
    """Frontend de origem permitida (ex.: os.agno.com -> localhost) é sempre cross-site para o navegador."""
    client = loopback_client(dev_app)

    response = client.post("/admin/refresh-cache", headers={"Origin": ALLOWED, "Sec-Fetch-Site": "cross-site"})

    assert response.status_code == 200 and response.json() == {"status": "no_cache"}
    assert response.headers["access-control-allow-origin"] == ALLOWED


def test_modo_dev_local_websocket_de_origem_nao_permitida_fecha_1008(dev_app: FastAPI):
    """Cross-site WebSocket hijacking: o navegador manda Origin no handshake."""
    url = "ws://127.0.0.1:7777/workflows/ws"
    with pytest.raises(WebSocketDisconnect) as exc:
        with loopback_client(dev_app).websocket_connect(url, headers={"Origin": "https://evil.example.com"}):
            pass  # pragma: no cover - a conexão nunca é aceita
    assert exc.value.code == 1008


# ── middleware isolado (escopos ASGI que o TestClient não monta) ────


async def _ok_app(scope: Scope, receive: Receive, send: Send) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})


async def _status(middleware: ApiKeyAuthMiddleware, scope: dict[str, Any]) -> int:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await middleware(scope, receive, send)
    return int(sent[0]["status"])


def _http_scope(
    *,
    method: str = "GET",
    path: str = "/agents",
    host: str | None = "127.0.0.1:7777",
    client: tuple[str, int] | None = ("127.0.0.1", 50000),
    server: tuple[str, int] | None = ("127.0.0.1", 7777),
    headers: list[tuple[bytes, bytes]] | None = None,
) -> dict[str, Any]:
    raw = list(headers or [])
    if host is not None:
        raw.append((b"host", host.encode("latin-1")))
    return {
        "type": "http",
        "method": method,
        "path": path,
        "root_path": "",
        "headers": raw,
        "client": client,
        "server": server,
    }


@pytest.mark.parametrize(
    ("overrides", "expected"),
    [
        ({}, 200),
        ({"client": None}, 401),  # socket UNIX (sem endereço de cliente): não suportado no modo dev
        ({"client": ("::1", 1), "server": ("::1", 7777), "host": "[::1]:7777"}, 200),
        ({"host": "localhost"}, 200),
        ({"host": "LOCALHOST:7777"}, 200),
        ({"host": "127.0.0.5"}, 200),
        ({"host": "[::1]"}, 200),
        ({"client": ("::ffff:127.0.0.1", 1)}, 401),  # IPv4 mapeado: negado (determinístico entre versões)
        ({"server": ("::ffff:127.0.0.1", 7777)}, 401),
        ({"client": ("10.0.0.2", 1)}, 401),
        ({"client": ("testclient", 1)}, 401),
        ({"server": None}, 401),
        ({"server": ("0.0.0.0", 7777)}, 401),  # noqa: S104 - endereço que precisa ser negado
        ({"host": None}, 401),
        ({"host": ""}, 401),
        ({"host": "localhost:abc"}, 401),
        ({"host": "[::1"}, 401),
        ({"host": "::1"}, 401),
        ({"host": "localhost."}, 401),
    ],
)
async def test_modo_dev_local_guarda_por_request(overrides: dict[str, Any], expected: int):
    middleware = ApiKeyAuthMiddleware(_ok_app, keys=None, docs_require_admin=False)

    assert await _status(middleware, _http_scope(**overrides)) == expected


@pytest.mark.parametrize(
    ("headers", "expected"),
    [
        ([], 200),
        ([(b"origin", ALLOWED.encode())], 200),
        ([(b"origin", ALLOWED.encode()), (b"sec-fetch-site", b"cross-site")], 200),
        ([(b"sec-fetch-site", b"same-origin")], 200),
        ([(b"sec-fetch-site", b"same-site")], 200),
        ([(b"sec-fetch-site", b"none")], 200),
        ([(b"origin", b"https://evil.example.com")], 401),
        ([(b"origin", b"null")], 401),
        ([(b"origin", (ALLOWED + "/").encode())], 401),
        ([(b"origin", ALLOWED.upper().encode())], 401),
        ([(b"sec-fetch-site", b"cross-site")], 401),
        ([(b"sec-fetch-site", b"Cross-Site")], 401),
    ],
)
async def test_modo_dev_local_guarda_de_origem(headers: list[tuple[bytes, bytes]], expected: int):
    middleware = ApiKeyAuthMiddleware(_ok_app, keys=None, docs_require_admin=False, allowed_origins=(ALLOWED,))

    scope = _http_scope(method="POST", path="/admin/refresh-cache", headers=headers)
    assert await _status(middleware, scope) == expected


async def test_modo_dev_local_sem_origens_permitidas_recusa_qualquer_origin():
    """Default do construtor é fail-closed: sem lista, nenhum Origin passa."""
    middleware = ApiKeyAuthMiddleware(_ok_app, keys=None, docs_require_admin=False)

    assert await _status(middleware, _http_scope(headers=[(b"origin", ALLOWED.encode())])) == 401
    assert await _status(middleware, _http_scope()) == 200


async def test_lifespan_passa_direto_no_modo_dev_local():
    seen: list[str] = []

    async def inner(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["type"])

    async def receive() -> Message:  # pragma: no cover - não chamado
        return {"type": "lifespan.startup"}

    async def send(message: Message) -> None:  # pragma: no cover - não chamado
        return None

    await ApiKeyAuthMiddleware(inner, keys=None, docs_require_admin=False)({"type": "lifespan"}, receive, send)
    assert seen == ["lifespan"]


async def test_preflight_que_chega_a_auth_exige_chave():
    """O CORS mais externo responde o preflight; se a ordem quebrar, a auth não abre exceção."""
    keys = ApiKeys(run=RUN_KEY.encode(), admin=ADMIN_KEY.encode())
    middleware = ApiKeyAuthMiddleware(_ok_app, keys=keys, docs_require_admin=True)
    preflight = [(b"origin", ALLOWED.encode()), (b"access-control-request-method", b"POST")]

    status = await _status(middleware, _http_scope(method="OPTIONS", path="/admin/health", headers=preflight))

    assert status == 401


# ── chaves fora dos logs ────────────────────────────────────────────


def test_chaves_nunca_aparecem_em_logs(
    build_app: Callable[..., FastAPI], caplog: pytest.LogCaptureFixture, capfd: pytest.CaptureFixture[str]
):
    caplog.set_level(logging.DEBUG)
    app = build_app(APP_HOST="0.0.0.0", ENVIRONMENT="production", **KEYS)  # noqa: S104
    client = TestClient(app, raise_server_exceptions=False)

    client.post("/agents/agente-1/runs", data={"message": "oi", "stream": "false"}, headers=_bearer(RUN_KEY))
    client.post("/agui", json=AGUI_BODY, headers={"X-API-Key": ADMIN_KEY})
    client.get("/admin/health", headers=_bearer(RUN_KEY))
    client.get("/admin/health", headers=_bearer(WRONG_KEY))
    client.get("/sessions", headers={"X-API-Key": WRONG_KEY})
    for headers in (_bearer(WRONG_KEY), _bearer(RUN_KEY)):
        try:
            with client.websocket_connect("/workflows/ws", headers=headers) as ws:
                ws.receive_json()
        except WebSocketDisconnect:
            pass
    with pytest.raises(ValueError) as short_key:
        build_app(with_agent_os=False, API_KEY_RUN=WRONG_KEY[:20], API_KEY_ADMIN=ADMIN_KEY)

    out, err = capfd.readouterr()
    captured = "\n".join([out, err, caplog.text, str(short_key.value)])
    for secret in (RUN_KEY, ADMIN_KEY, WRONG_KEY, WRONG_KEY[:20]):
        assert secret not in captured
