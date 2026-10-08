"""QA do F1-04: auth na pilha real (métricas, spans) e com servidor uvicorn real em loopback.

Sem LLM real, sem rede externa: o uvicorn sobe só em 127.0.0.1 (porta livre) com o app real
(``AppFactory`` + ``_mount_agent_os`` com Agent/Team de ``FakeChatModel``).
"""

from __future__ import annotations

import asyncio
import socket
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager

import httpx
import pytest
import uvicorn
from agno.agent import Agent
from agno.team import Team
from fastapi import FastAPI
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from starlette.websockets import WebSocketDisconnect
from websockets.exceptions import InvalidStatus
from websockets.sync.client import connect as ws_connect

from src.infrastructure.web import metrics_middleware
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import FakeChatModel, RecordingTelemetryMetrics, loopback_client

ALL_INTERFACES = "0.0.0" + ".0"  # só valor de APP_HOST no ambiente: nenhum teste faz bind nele
RUN_KEY = "qa-run-key-" + "r" * 21
ADMIN_KEY = "qa-admin-key-" + "a" * 19
KEYS = {"API_KEY_RUN": RUN_KEY, "API_KEY_ADMIN": ADMIN_KEY}
_ENV_NAMES = ("ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS", "APP_HOST", "API_KEY_RUN", "API_KEY_ADMIN")
AGUI_BODY = {
    "threadId": "t1",
    "runId": "r1",
    "state": {},
    "messages": [{"id": "m1", "role": "user", "content": "oi"}],
    "tools": [],
    "context": [],
    "forwardedProps": {},
}


def _bearer(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _entities(responses: list[str] | None = None) -> tuple[Agent, Team]:
    answers = responses if responses is not None else ["resposta do agente"] * 20
    agent = Agent(id="agente-1", name="Agente 1", model=FakeChatModel(responses=list(answers)), telemetry=False)
    member = Agent(id="membro-1", name="Membro", model=FakeChatModel(responses=["membro"] * 20), telemetry=False)
    team = Team(
        id="time-1",
        name="Time 1",
        members=[member],
        model=FakeChatModel(responses=[a.replace("agente", "time") for a in answers]),
        telemetry=False,
    )
    return agent, team


@pytest.fixture
def build_app(monkeypatch: pytest.MonkeyPatch) -> Callable[..., FastAPI]:
    def _build(responses: list[str] | None = None, **env: str) -> FastAPI:
        for name in _ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("AGNO_TELEMETRY", "false")
        for name, value in env.items():
            monkeypatch.setenv(name, value)
        factory = AppFactory()
        app = factory.create_app()
        agent, team = _entities(responses)
        factory._mount_agent_os(app, [agent], [team])
        return app

    return _build


# ── métricas: request recusado pela auth não deixa rastro ────────────


@pytest.fixture
def metrics(monkeypatch: pytest.MonkeyPatch) -> RecordingTelemetryMetrics:
    recording = RecordingTelemetryMetrics()
    monkeypatch.setattr(metrics_middleware, "TelemetryMetrics", lambda: recording)
    return recording


def _no_series(m: RecordingTelemetryMetrics) -> bool:
    return (
        not m.agent_requests
        and not m.team_requests
        and not m.request_statuses
        and not m.agent_errors
        and not m.team_errors
        and not any(m.active.values())
        and not m.active
        and not m.durations
    )


RUN_BODIES = [
    ("/agents/agente-1/runs", {"message": "oi", "stream": "false"}),
    ("/agents/agente-1/runs", {"message": "oi", "stream": "true"}),
    ("/agents/inventado-123/runs", {"message": "oi", "stream": "false"}),
    ("/agents/agente-1/runs", {"stream": "false"}),  # 422 se passasse da auth
    ("/teams/time-1/runs", {"message": "oi", "stream": "false"}),
    ("/teams/time-1/runs", {"message": "oi", "stream": "true"}),
    ("/teams/inventado-123/runs", {"message": "oi", "stream": "true"}),
]


@pytest.mark.parametrize(("path", "form"), RUN_BODIES)
@pytest.mark.parametrize(
    "headers",
    [{}, _bearer("errada" + "x" * 30), {"X-API-Key": ""}, {"Authorization": "Basic Zm9vOmJhcg=="}],
    ids=["sem-chave", "chave-errada", "x-api-key-vazia", "basic"],
)
def test_run_recusado_por_401_nao_cria_serie_nem_conta_erro(
    build_app: Callable[..., FastAPI],
    metrics: RecordingTelemetryMetrics,
    path: str,
    form: dict[str, str],
    headers: dict[str, str],
):
    client = loopback_client(
        build_app(APP_HOST=ALL_INTERFACES, ENVIRONMENT="production", **KEYS), raise_server_exceptions=False
    )

    response = client.post(path, data=form, headers=headers)

    assert response.status_code == 401
    assert _no_series(metrics), (dict(metrics.request_statuses), dict(metrics.active))


def test_run_recusado_no_modo_dev_local_por_cliente_externo_nao_cria_serie(
    build_app: Callable[..., FastAPI], metrics: RecordingTelemetryMetrics
):
    app = build_app(ENVIRONMENT="development")
    client = loopback_client(app, client=("203.0.113.9", 4000), raise_server_exceptions=False)

    for path, form in RUN_BODIES:
        assert client.post(path, data=form).status_code == 401

    assert _no_series(metrics)


def test_chave_run_em_rota_admin_nao_conta_como_run(
    build_app: Callable[..., FastAPI], metrics: RecordingTelemetryMetrics
):
    client = loopback_client(
        build_app(APP_HOST=ALL_INTERFACES, ENVIRONMENT="production", **KEYS), raise_server_exceptions=False
    )

    assert client.post("/admin/refresh-cache", headers=_bearer(RUN_KEY)).status_code == 403
    assert client.get("/metrics/cache", headers=_bearer(RUN_KEY)).status_code == 403

    assert _no_series(metrics)


def test_run_autenticado_conta_uma_serie_e_run_falho_conta_um_erro(
    build_app: Callable[..., FastAPI], metrics: RecordingTelemetryMetrics
):
    """Com chave valida, o MetricsMiddleware (dentro da auth) segue medindo; a auth não o desliga."""
    ok_client = loopback_client(
        build_app(["ok"] * 5, APP_HOST=ALL_INTERFACES, ENVIRONMENT="production", **KEYS),
        raise_server_exceptions=False,
    )

    ok = ok_client.post("/agents/agente-1/runs", data={"message": "oi", "stream": "false"}, headers=_bearer(RUN_KEY))
    team = ok_client.post("/teams/time-1/runs", data={"message": "oi", "stream": "true"}, headers=_bearer(ADMIN_KEY))

    assert ok.status_code == 200 and team.status_code == 200
    assert metrics.request_statuses == {("agente-1", "success"): 1, ("time-1", "success"): 1}
    assert not metrics.agent_errors and not metrics.team_errors

    failing = loopback_client(
        build_app([], APP_HOST=ALL_INTERFACES, ENVIRONMENT="production", **KEYS),
        raise_server_exceptions=False,
    )
    bad = failing.post("/agents/agente-1/runs", data={"message": "oi", "stream": "true"}, headers=_bearer(RUN_KEY))

    assert bad.status_code == 200
    assert metrics.agent_errors == {"agente-1": 1}


def test_run_de_id_inexistente_com_chave_valida_usa_unknown(
    build_app: Callable[..., FastAPI], metrics: RecordingTelemetryMetrics
):
    client = loopback_client(
        build_app(APP_HOST=ALL_INTERFACES, ENVIRONMENT="production", **KEYS), raise_server_exceptions=False
    )

    assert client.post("/agents/inventado/runs", data={"message": "oi"}, headers=_bearer(RUN_KEY)).status_code == 404

    assert metrics.agent_errors == {"unknown": 1}
    assert "inventado" not in metrics.agent_requests


# ── spans: a chave nunca vira atributo ───────────────────────────────


def test_spans_nao_carregam_a_chave(
    build_app: Callable[..., FastAPI], monkeypatch: pytest.MonkeyPatch, reset_otel_providers: None
):
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    client = loopback_client(
        build_app(APP_HOST=ALL_INTERFACES, ENVIRONMENT="production", **KEYS),
        raise_server_exceptions=False,
    )

    client.post("/agents/agente-1/runs", data={"message": "oi", "stream": "false"}, headers=_bearer(RUN_KEY))
    client.post("/agui", json=AGUI_BODY, headers={"X-API-Key": ADMIN_KEY})
    client.get("/metrics", headers=_bearer(RUN_KEY))  # /admin/health e /metrics/cache são excluídos do OTel
    client.get("/agents", headers=_bearer("errada" + "x" * 30))
    client.get("/agents")

    spans = exporter.get_finished_spans()
    assert spans, "a instrumentação deveria gerar spans (inclusive para 401/403)"
    dump = repr(
        [(s.name, dict(s.attributes or {}), [(e.name, dict(e.attributes or {})) for e in s.events]) for s in spans]
    )
    assert RUN_KEY not in dump and ADMIN_KEY not in dump and "errada" not in dump
    statuses = {s.attributes.get("http.status_code") for s in spans if s.attributes}
    assert {401, 403} <= statuses  # a auth fica DENTRO da instrumentação: o span do 401/403 existe


# ── servidor uvicorn real ────────────────────────────────────────────


def _raw_ws_handshake_status(port: int, *, host: str, address: str = "127.0.0.1") -> int:
    """Handshake WebSocket cru (o cliente `websockets` não deixa trocar o Host); devolve o status."""
    request = (
        f"GET /workflows/ws HTTP/1.1\r\nHost: {host}\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n\r\n"
    )
    with socket.create_connection((address, port), timeout=10) as sock:
        sock.sendall(request.encode())
        return int(sock.recv(4096).split(b" ", 2)[1])


@contextmanager
def _serve(app: FastAPI, host: str) -> Iterator[int]:
    """uvicorn real num socket já ligado a uma porta efêmera.

    O socket é entregue ao uvicorn ainda aberto: sem a janela entre "achar porta livre" e
    o bind do servidor, em que outro worker do pytest-xdist poderia tomar a mesma porta.
    """
    listener = socket.socket()
    listener.bind((host, 0))
    port = int(listener.getsockname()[1])
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="off", ws="websockets-sansio")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve(sockets=[listener])), daemon=True)
    try:
        thread.start()
        deadline = time.monotonic() + 15
        while not server.started:
            if time.monotonic() > deadline or not thread.is_alive():
                raise RuntimeError("uvicorn de teste não subiu")
            time.sleep(0.02)
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        listener.close()


@pytest.fixture
def keyed_server(build_app: Callable[..., FastAPI]) -> Iterator[str]:
    app = build_app(
        APP_HOST="127.0.0.1", ENVIRONMENT="production", CORS_ALLOWED_ORIGINS="https://painel.example.com", **KEYS
    )
    with _serve(app, "127.0.0.1") as port:
        yield f"127.0.0.1:{port}"


def test_uvicorn_real_com_chaves_fluxo_completo(keyed_server: str):
    base = f"http://{keyed_server}"
    with httpx.Client(base_url=base, timeout=20) as client:
        assert client.get("/livez").json() == {"status": "ok"}
        assert client.get("/agents").status_code == 401
        assert client.get("/agents", headers=_bearer(RUN_KEY)).status_code == 200
        assert client.get("/admin/health", headers=_bearer(RUN_KEY)).status_code == 403
        assert client.get("/admin/health", headers=_bearer(ADMIN_KEY)).status_code == 200

        run = client.post("/agents/agente-1/runs", data={"message": "oi", "stream": "false"}, headers=_bearer(RUN_KEY))
        assert run.status_code == 200 and run.json()["content"] == "resposta do agente"

        with client.stream(
            "POST", "/agents/agente-1/runs", data={"message": "oi", "stream": "true"}, headers=_bearer(RUN_KEY)
        ) as sse:
            text = "".join(sse.iter_text())
        assert sse.status_code == 200 and "RunCompleted" in text and "resposta do agente" in text

        with client.stream("POST", "/agui", json=AGUI_BODY, headers={"X-API-Key": RUN_KEY}) as agui:
            agui_text = "".join(agui.iter_text())
        assert agui.status_code == 200 and "RUN_FINISHED" in agui_text
        assert client.post("/agui", json=AGUI_BODY).status_code == 401

        team = client.post("/teams/time-1/runs", data={"message": "oi", "stream": "false"}, headers=_bearer(ADMIN_KEY))
        assert team.status_code == 200 and team.json()["content"] == "resposta do time"

        preflight = client.options(
            "/agui",
            headers={"Origin": "https://painel.example.com", "Access-Control-Request-Method": "POST"},
        )
        assert preflight.status_code == 200


def test_uvicorn_real_websocket_com_e_sem_chave(keyed_server: str):
    url = f"ws://{keyed_server}/workflows/ws"

    for headers in ({}, _bearer("errada" + "x" * 30), {"X-API-Key": "curta"}):
        with pytest.raises(InvalidStatus) as exc:
            ws_connect(url, additional_headers=headers, open_timeout=10)
        assert exc.value.response.status_code == 403  # close antes do accept -> 403 no handshake

    for headers in (_bearer(RUN_KEY), {"X-API-Key": ADMIN_KEY}):
        with ws_connect(url, additional_headers=headers, open_timeout=10) as ws:
            assert '"connected"' in str(ws.recv(timeout=10))


def test_uvicorn_real_sem_chaves_loopback_atende_e_host_estranho_nao(build_app: Callable[..., FastAPI]):
    app = build_app(APP_HOST="127.0.0.1", ENVIRONMENT="development")
    with _serve(app, "127.0.0.1") as port:
        base = f"http://127.0.0.1:{port}"
        assert httpx.get(f"{base}/agents", timeout=10).status_code == 200
        assert httpx.get(f"{base}/admin/health", timeout=10).status_code == 200
        assert httpx.get(f"{base}/agents", headers={"Host": "evil.example.com"}, timeout=10).status_code == 401
        assert httpx.get(f"{base}/agents", headers={"Host": f"localhost:{port}"}, timeout=10).status_code == 200
        assert httpx.get(f"{base}/livez", headers={"Host": "evil.example.com"}, timeout=10).status_code == 200
        # uvicorn confia em X-Forwarded-For do peer 127.0.0.1 e reescreve o client: IP externo => 401
        assert httpx.get(f"{base}/agents", headers={"X-Forwarded-For": "8.8.8.8"}, timeout=10).status_code == 401
        url = f"ws://127.0.0.1:{port}/workflows/ws"
        with ws_connect(url, open_timeout=10) as ws:
            assert '"connected"' in str(ws.recv(timeout=10))
        assert _raw_ws_handshake_status(port, host="evil.example.com") == 403  # DNS rebinding
        assert _raw_ws_handshake_status(port, host=f"127.0.0.1:{port}") == 101


# ── acesso pela interface de rede (scope ASGI; nenhum bind fora do loopback) ──
# `uvicorn --host 0.0.0.0` entrega no scope o endereço local real do socket (`server`) e o
# do peer (`client`). Simular os dois evita expor a máquina do dev/CI na rede a cada execução.

LAN_BASE_URL = "http://192.0.2.10:7777"  # TEST-NET-1 (RFC 5737): endereço local da "interface de rede"
LAN_CLIENT = ("192.0.2.20", 40000)


def test_0000_direto_sem_chaves_recusa_acesso_pela_interface_de_rede(build_app: Callable[..., FastAPI]):
    """`uvicorn app:app --host 0.0.0.0` com APP_HOST ausente: o app sobe (default 127.0.0.1),
    mas o request que entra pela interface de rede é recusado pela guarda por request."""
    app = build_app(ENVIRONMENT="development")  # APP_HOST ausente => 127.0.0.1 no AppConfig
    local = loopback_client(app)
    lan = loopback_client(app, base_url=LAN_BASE_URL, client=LAN_CLIENT)

    assert local.get("/agents").status_code == 200
    assert lan.get("/agents").status_code == 401
    assert lan.post("/admin/refresh-cache").status_code == 401
    assert lan.get("/livez").status_code == 200  # público
    # Host forjado para loopback não ajuda: server/client continuam sendo da interface de rede
    assert lan.get("/agents", headers={"Host": "localhost"}).status_code == 401
    with pytest.raises(WebSocketDisconnect) as exc:
        with lan.websocket_connect("ws://192.0.2.10:7777/workflows/ws"):
            pass  # pragma: no cover - a conexão nunca é aceita
    assert exc.value.code == 1008


def test_0000_com_chaves_atende_pela_rede_so_com_credencial(build_app: Callable[..., FastAPI]):
    app = build_app(APP_HOST=ALL_INTERFACES, ENVIRONMENT="production", **KEYS)
    lan = loopback_client(app, base_url=LAN_BASE_URL, client=LAN_CLIENT)

    assert lan.get("/agents").status_code == 401
    assert lan.get("/agents", headers=_bearer(RUN_KEY)).status_code == 200
    assert lan.get("/admin/health", headers=_bearer(RUN_KEY)).status_code == 403


# ── BUG-F1-04-DEV-CSRF ───────────────────────────────────────────────


@pytest.mark.parametrize(
    ("method", "path", "data"),
    [
        ("POST", "/admin/refresh-cache", None),
        ("POST", "/databases/all/migrate", None),
        ("POST", "/agents/agente-1/runs", {"message": "oi", "stream": "false"}),  # multipart: request simples
    ],
)
def test_modo_dev_local_nao_executa_request_cross_site_de_navegador(
    build_app: Callable[..., FastAPI], method: str, path: str, data: dict[str, str] | None
):
    client = loopback_client(build_app(APP_HOST="127.0.0.1", ENVIRONMENT="development"))

    response = client.request(
        method,
        path,
        data=data,
        headers={"Origin": "https://evil.example.com", "Sec-Fetch-Site": "cross-site", "Sec-Fetch-Mode": "no-cors"},
    )

    assert response.status_code in (401, 403)
