"""Testes do middleware ASGI de métricas de negócio (agentes/teams)."""

from __future__ import annotations

import asyncio
import contextlib
import json
from collections.abc import Iterator, MutableMapping
from typing import Any

import pytest
import structlog
from fastapi import FastAPI
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from starlette.testclient import TestClient
from starlette.types import ASGIApp, Message, Receive, Scope, Send
from structlog.testing import capture_logs

from src.infrastructure.telemetry import metrics as metrics_module
from src.infrastructure.web import metrics_middleware
from src.infrastructure.web.metrics_middleware import MetricsMiddleware
from tests.fakes.telemetry import RecordingTelemetryMetrics

AGENT_PATH = "/agents/agente-1/runs"
TEAM_PATH = "/teams/time-1/runs"
SSE = b"text/event-stream; charset=utf-8"
JSON = b"application/json"


class _Boom(Exception):
    pass


def _sse(event: str) -> bytes:
    """Evento no formato de ``agno.os.utils.format_sse_event``."""
    return f'event: {event}\ndata: {{"event":"{event}"}}\n\n'.encode()


def _app_responding(
    status: int,
    *,
    chunks: tuple[bytes, ...] = (b"",),
    content_type: bytes = JSON,
    then: BaseException | None = None,
) -> ASGIApp:
    """App ASGI mínima: envia ``status`` e ``chunks``; opcionalmente levanta ``then`` após o 1o chunk."""

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send({"type": "http.response.start", "status": status, "headers": [(b"content-type", content_type)]})
        for index, chunk in enumerate(chunks):
            await send({"type": "http.response.body", "body": chunk, "more_body": index < len(chunks) - 1})
            if then is not None:
                raise then

    return app


def _app_raising(exc: BaseException) -> ASGIApp:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise exc

    return app


def _call(
    app: ASGIApp,
    metrics: RecordingTelemetryMetrics,
    path: str = AGENT_PATH,
    *,
    method: str = "POST",
    scope_type: str = "http",
) -> list[Message]:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    middleware = MetricsMiddleware(app, metrics=metrics)
    asyncio.run(middleware({"type": scope_type, "method": method, "path": path}, receive, send))
    return sent


@pytest.fixture
def metrics() -> RecordingTelemetryMetrics:
    return RecordingTelemetryMetrics()


@pytest.fixture
def logs(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[MutableMapping[str, Any]]]:
    """Captura os logs do middleware.

    Com ``cache_logger_on_first_use=True`` (structlog_logger.py), o ``_log`` do módulo fica
    preso à lista de processors de um ``structlog.configure`` anterior (de outro teste) e o
    ``capture_logs`` não o enxerga; um proxy novo resolve a config vigente no 1o uso.
    """
    monkeypatch.setattr(metrics_middleware, "_log", structlog.get_logger(metrics_middleware.__name__))
    with capture_logs() as captured:
        yield captured


# ── sucesso e status HTTP ───────────────────────────────────────────


def test_sucesso_registra_request_duracao_e_libera_active(metrics: RecordingTelemetryMetrics) -> None:
    sent = _call(_app_responding(200, chunks=(b'{"status":"COMPLETED"}',)), metrics)

    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert sent[1]["body"] == b'{"status":"COMPLETED"}'  # corpo repassado intacto
    assert metrics.agent_requests == {"agente-1": 1}
    assert metrics.active == {"agente-1": 0}
    assert metrics.agent_errors == {}
    assert [agent for agent, _ in metrics.durations] == ["agente-1"]


@pytest.mark.parametrize(("status", "metric_id"), [(400, "unknown"), (422, "unknown"), (500, "agente-1")])
def test_status_de_erro_conta_um_erro(metrics: RecordingTelemetryMetrics, status: int, metric_id: str) -> None:
    _call(_app_responding(status), metrics)

    assert metrics.agent_errors == {metric_id: 1}
    assert metrics.active == {"agente-1": 0}


@pytest.mark.parametrize("status", [400, 404, 422, 499])
@pytest.mark.parametrize(("path", "is_agent"), [(AGENT_PATH, True), (TEAM_PATH, False)])
def test_4xx_usa_id_unknown(metrics: RecordingTelemetryMetrics, path: str, is_agent: bool, status: int) -> None:
    """Qualquer 4xx (ex.: 422 do FastAPI antes de buscar o agente) não cria série com o id do path."""
    _call(_app_responding(status, chunks=(b'{"detail":"erro"}',)), metrics, path=path)

    if is_agent:
        assert metrics.agent_requests == {"unknown": 1}
        assert metrics.agent_errors == {"unknown": 1}
        assert [agent for agent, _ in metrics.durations] == ["unknown"]
        assert metrics.active == {"agente-1": 0}  # o +1 foi antes de saber o status
    else:
        assert metrics.team_requests == {"unknown": 1}
        assert metrics.team_errors == {"unknown": 1}
    assert metrics.request_statuses == {("unknown", "error"): 1}


@pytest.mark.parametrize("status", [500, 503])
def test_5xx_mantem_o_id_do_path(metrics: RecordingTelemetryMetrics, status: int) -> None:
    _call(_app_responding(status), metrics)

    assert metrics.agent_errors == {"agente-1": 1}
    assert metrics.request_statuses == {("agente-1", "error"): 1}


@pytest.mark.parametrize(("path", "entity"), [(AGENT_PATH, "agente-1"), (TEAM_PATH, "time-1")])
@pytest.mark.parametrize(
    ("app", "expected_status"),
    [
        (_app_responding(200, chunks=(b'{"status":"COMPLETED"}',)), "success"),
        (_app_responding(200, chunks=(b'{"status":"ERROR"}',)), "error"),
        (_app_responding(200, chunks=(_sse("RunError") + _sse("TeamRunError"),), content_type=SSE), "error"),
        (_app_responding(500), "error"),
        (_app_raising(_Boom("falha")), "error"),
        (_app_raising(asyncio.CancelledError()), "success"),
    ],
    ids=["sucesso", "json-error", "sse-error", "500", "excecao", "cancelado"],
)
def test_request_leva_status_do_run(
    metrics: RecordingTelemetryMetrics, path: str, entity: str, app: ASGIApp, expected_status: str
) -> None:
    with contextlib.suppress(_Boom, asyncio.CancelledError):
        _call(app, metrics, path=path)

    assert metrics.request_statuses == {(entity, expected_status): 1}


# ── falha de run com status 200 (Agno captura a exceção) ────────────


def test_sse_com_run_error_conta_um_erro(
    metrics: RecordingTelemetryMetrics, logs: list[MutableMapping[str, Any]]
) -> None:
    chunks = (_sse("RunStarted"), _sse("RunError"), _sse("RunError"))
    _call(_app_responding(200, chunks=chunks, content_type=SSE), metrics)

    assert metrics.agent_errors == {"agente-1": 1}
    assert metrics.active == {"agente-1": 0}
    erros = [log for log in logs if log["log_level"] == "error"]
    assert [(log["entity_id"], log["reason"]) for log in erros] == [("agente-1", "evento RunError")]


def test_sse_com_evento_partido_entre_chunks(metrics: RecordingTelemetryMetrics) -> None:
    stream = _sse("RunStarted") + _sse("RunError")
    cut = len(_sse("RunStarted")) + len("event: Run")
    _call(_app_responding(200, chunks=(stream[:cut], stream[cut:]), content_type=SSE), metrics)

    assert metrics.agent_errors == {"agente-1": 1}


def test_sse_sem_erro_e_dados_que_citam_runerror_nao_contam(metrics: RecordingTelemetryMetrics) -> None:
    content = b'event: RunContent\ndata: {"content":"event: RunError"}\n\n'
    _call(_app_responding(200, chunks=(_sse("RunStarted"), content, _sse("RunCompleted")), content_type=SSE), metrics)

    assert metrics.agent_errors == {}


def test_sse_de_team_conta_team_run_error_e_ignora_erro_de_membro(metrics: RecordingTelemetryMetrics) -> None:
    _call(_app_responding(200, chunks=(_sse("RunError"),), content_type=SSE), metrics, path=TEAM_PATH)
    assert metrics.team_errors == {}  # membro falhou, o team pode se recuperar

    team_failed = _app_responding(200, chunks=(_sse("RunError"), _sse("TeamRunError")), content_type=SSE)
    _call(team_failed, metrics, path=TEAM_PATH)
    assert metrics.team_errors == {"time-1": 1}
    assert metrics.team_requests == {"time-1": 2}
    assert metrics.agent_errors == {}


def test_outro_content_type_nao_e_inspecionado(metrics: RecordingTelemetryMetrics) -> None:
    _call(_app_responding(200, chunks=(_sse("RunError"),), content_type=b"text/plain"), metrics)

    assert metrics.agent_errors == {}


def test_sse_de_agente_nao_conta_team_run_error(metrics: RecordingTelemetryMetrics) -> None:
    _call(_app_responding(200, chunks=(_sse("TeamRunError"),), content_type=SSE), metrics)

    assert metrics.agent_errors == {}


@pytest.mark.parametrize(("path", "is_agent"), [(AGENT_PATH, True), (TEAM_PATH, False)])
def test_json_com_status_error_conta_um_erro(metrics: RecordingTelemetryMetrics, path: str, is_agent: bool) -> None:
    body = json.dumps({"run_id": "r1", "content": "falhou", "status": "ERROR"}).encode()
    _call(_app_responding(200, chunks=(body[:10], body[10:])), metrics, path=path)

    errors = metrics.agent_errors if is_agent else metrics.team_errors
    assert errors == {path.split("/")[2]: 1}


@pytest.mark.parametrize(
    "body",
    [
        json.dumps({"status": "COMPLETED", "member_responses": [{"status": "ERROR"}]}).encode(),
        b"[]",
        b"nao e json",
    ],
    ids=["erro-so-de-membro", "lista", "json-invalido"],
)
def test_json_sem_status_error_no_topo_nao_conta(metrics: RecordingTelemetryMetrics, body: bytes) -> None:
    _call(_app_responding(200, chunks=(body,)), metrics)

    assert metrics.agent_errors == {}
    assert metrics.agent_requests == {"agente-1": 1}


def test_json_acima_do_limite_nao_e_lido(
    metrics: RecordingTelemetryMetrics, logs: list[MutableMapping[str, Any]], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(metrics_middleware, "_JSON_BUFFER_LIMIT", 16)
    body = json.dumps({"content": "x" * 32, "status": "ERROR"}).encode()

    _call(_app_responding(200, chunks=(body[:20], body[20:])), metrics)

    assert metrics.agent_errors == {}
    assert [log["log_level"] for log in logs] == ["warning"]


# ── exceção e cancelamento ──────────────────────────────────────────


def test_excecao_antes_da_resposta_conta_um_erro_e_loga_so_o_tipo(
    metrics: RecordingTelemetryMetrics, logs: list[MutableMapping[str, Any]]
) -> None:
    with pytest.raises(_Boom):
        _call(_app_raising(_Boom("segredo-no-texto")), metrics)

    assert metrics.agent_errors == {"agente-1": 1}
    assert metrics.active == {"agente-1": 0}
    assert len(metrics.durations) == 1
    (log,) = logs
    assert (log["log_level"], log["error_type"], log["entity_id"], log["status_code"]) == (
        "error",
        "_Boom",
        "agente-1",
        None,
    )
    assert "segredo-no-texto" not in repr(log)


@pytest.mark.parametrize(
    ("status", "content_type", "first_chunk"),
    [(200, SSE, _sse("RunError")), (200, JSON, b"{"), (500, JSON, b"{")],
    ids=["sse-com-run-error", "json-200", "json-500"],
)
def test_excecao_no_meio_da_resposta_conta_um_unico_erro(
    metrics: RecordingTelemetryMetrics, status: int, content_type: bytes, first_chunk: bytes
) -> None:
    app = _app_responding(status, chunks=(first_chunk, b"}"), content_type=content_type, then=_Boom("falha"))
    with pytest.raises(_Boom):
        _call(app, metrics)

    assert metrics.agent_errors == {"agente-1": 1}
    assert metrics.active == {"agente-1": 0}


@pytest.mark.parametrize(
    "app",
    [
        _app_raising(asyncio.CancelledError()),
        _app_responding(200, chunks=(_sse("RunStarted"), b""), content_type=SSE, then=asyncio.CancelledError()),
    ],
    ids=["antes-da-resposta", "no-meio-do-stream"],
)
def test_cancelamento_libera_active_e_nao_conta_erro(metrics: RecordingTelemetryMetrics, app: ASGIApp) -> None:
    with pytest.raises(asyncio.CancelledError):
        _call(app, metrics)

    assert metrics.active == {"agente-1": 0}
    assert metrics.agent_errors == {}
    assert metrics.agent_requests == {"agente-1": 1}
    assert len(metrics.durations) == 1


# ── quais requests são runs ─────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "method", "scope_type"),
    [
        ("/admin/health", "GET", "http"),
        ("/agents/agente-1", "POST", "http"),
        (AGENT_PATH, "GET", "http"),  # listagem de runs
        ("/agents/agente-1/runs/r1/cancel", "POST", "http"),
        ("/agents/agente-1/runs/r1/continue", "POST", "http"),
        ("/teams/time-1/runs/r1/cancel", "POST", "http"),
        (AGENT_PATH, "GET", "websocket"),
    ],
)
def test_fora_de_run_nao_registra_metricas(
    metrics: RecordingTelemetryMetrics, path: str, method: str, scope_type: str
) -> None:
    calls: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        calls.append(scope["path"])

    _call(app, metrics, path=path, method=method, scope_type=scope_type)

    assert calls == [path]
    assert (metrics.agent_requests, metrics.team_requests, metrics.active) == ({}, {}, {})


@pytest.mark.parametrize("path", ["/playground/agents/agente-1/runs", "/agents/agente-1/runs/"])
def test_variantes_do_path_de_run_sao_reconhecidas(metrics: RecordingTelemetryMetrics, path: str) -> None:
    _call(_app_responding(200), metrics, path=path)

    assert metrics.agent_requests == {"agente-1": 1}


def test_team_registra_request_sem_metricas_de_agente(metrics: RecordingTelemetryMetrics) -> None:
    _call(_app_responding(200), metrics, path=TEAM_PATH)

    assert metrics.team_requests == {"time-1": 1}
    assert metrics.agent_requests == {}
    assert metrics.active == {}


# ── integração com FastAPI ──────────────────────────────────────────


def _fastapi_app(metrics: RecordingTelemetryMetrics) -> FastAPI:
    app = FastAPI()
    app.add_middleware(MetricsMiddleware, metrics=metrics)

    @app.post("/agents/{agent_id}/runs")
    async def agent_run(agent_id: str, falhar: bool = False) -> dict[str, str]:
        if falhar:
            raise ValueError("Erro simulado")
        return {"agent_id": agent_id, "status": "COMPLETED"}

    return app


@pytest.mark.parametrize(
    ("query", "expected_status", "expected_errors"),
    [("", 200, {}), ("?falhar=true", 500, {"agente-1": 1})],
)
def test_fastapi_rota_real(
    metrics: RecordingTelemetryMetrics, query: str, expected_status: int, expected_errors: dict[str, int]
) -> None:
    client = TestClient(_fastapi_app(metrics), raise_server_exceptions=False)

    response = client.post(AGENT_PATH + query)

    assert response.status_code == expected_status
    assert metrics.agent_errors == expected_errors
    assert metrics.agent_requests == {"agente-1": 1}
    assert metrics.active == {"agente-1": 0}


# ── TelemetryMetrics real ───────────────────────────────────────────


def _install_real_instruments(monkeypatch: pytest.MonkeyPatch) -> tuple[MeterProvider, InMemoryMetricReader]:
    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    meter = meter_provider.get_meter("teste")
    for attr, name in [
        ("agent_requests_total", "agent_requests_total"),
        ("agent_errors_total", "agent_errors_total"),
        ("team_requests_total", "team_requests_total"),
        ("team_errors_total", "team_errors_total"),
    ]:
        monkeypatch.setattr(metrics_module, attr, meter.create_counter(name))
    monkeypatch.setattr(metrics_module, "agents_active", meter.create_up_down_counter("agents_active"))
    monkeypatch.setattr(
        metrics_module, "agent_response_duration", meter.create_histogram("agent_response_duration_seconds")
    )
    return meter_provider, reader


def _read(reader: InMemoryMetricReader) -> dict[str, list[tuple[dict[str, object], object]]]:
    data = reader.get_metrics_data()
    return {
        metric.name: [(dict(p.attributes or {}), getattr(p, "value", None)) for p in metric.data.data_points]
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for metric in sm.metrics
    }


def test_metricas_reais_agents_active_volta_a_zero_apos_cancelamento(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sem fake: TelemetryMetrics padrão escrevendo em instrumentos do SDK lidos por InMemoryMetricReader."""
    meter_provider, reader = _install_real_instruments(monkeypatch)

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        return None

    middleware = MetricsMiddleware(_app_raising(asyncio.CancelledError()))
    with pytest.raises(asyncio.CancelledError):
        asyncio.run(middleware({"type": "http", "method": "POST", "path": AGENT_PATH}, receive, send))

    values = _read(reader)
    meter_provider.shutdown()
    assert values["agents_active"] == [({"agent_id": "agente-1"}, 0)]
    assert values["agent_requests_total"] == [({"agent_id": "agente-1", "status": "success"}, 1)]
    assert "agent_errors_total" not in values


def test_metricas_reais_team_error(monkeypatch: pytest.MonkeyPatch) -> None:
    meter_provider, reader = _install_real_instruments(monkeypatch)

    middleware = MetricsMiddleware(_app_responding(200, chunks=(_sse("TeamRunError"),), content_type=SSE))

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        return None

    asyncio.run(middleware({"type": "http", "method": "POST", "path": TEAM_PATH}, receive, send))

    values = _read(reader)
    meter_provider.shutdown()
    assert values["team_errors_total"] == [({"team_id": "time-1"}, 1)]
    assert values["team_requests_total"] == [({"team_id": "time-1", "status": "error"}, 1)]
