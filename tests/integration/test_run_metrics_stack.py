"""Pilha real do AppFactory (CORS + MetricsMiddleware + instrument_app) com AgentOS e Agno reais (QA F1-01)."""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from agno.agent import Agent
from agno.team import Team
from opentelemetry import trace
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import SpanKind
from starlette.testclient import TestClient

from src.infrastructure.web import metrics_middleware
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import FakeChatModel, RecordingTelemetryMetrics, loopback_client
from tests.fakes.web import mount_agent_os

ORIGIN = "http://localhost:3000"  # origem do default de CORS_ALLOWED_ORIGINS (AppConfig)


@pytest.fixture
def metrics(monkeypatch: pytest.MonkeyPatch) -> RecordingTelemetryMetrics:
    recording = RecordingTelemetryMetrics()
    monkeypatch.setattr(metrics_middleware, "TelemetryMetrics", lambda: recording)
    return recording


@pytest.fixture
def exporter(reset_otel_providers: None) -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    trace.set_tracer_provider(provider)
    yield exporter


@pytest.fixture
def client(
    metrics: RecordingTelemetryMetrics, exporter: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> TestClient:
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    agent = Agent(id="agente-1", name="Agente 1", model=FakeChatModel(responses=[]), telemetry=False)
    member = Agent(id="membro-1", name="Membro", model=FakeChatModel(responses=[]), telemetry=False)
    team = Team(id="time-1", name="Time 1", members=[member], model=FakeChatModel(responses=[]), telemetry=False)
    factory = AppFactory()
    app = factory.create_app()
    mount_agent_os(factory, app, [agent], [team])  # o mesmo passo do lifespan, sem Mongo
    return loopback_client(app, raise_server_exceptions=False)


def _server_spans(exporter: InMemorySpanExporter) -> list[str]:
    return [str(s.attributes.get("http.route")) for s in exporter.get_finished_spans() if s.kind is SpanKind.SERVER]


@pytest.mark.parametrize("stream", ["true", "false"])
def test_falha_de_run_pela_pilha_do_app_conta_um_erro_e_gera_span_server(
    client: TestClient, metrics: RecordingTelemetryMetrics, exporter: InMemorySpanExporter, stream: str
) -> None:
    form = {"message": "oi", "stream": stream}
    response = client.post("/agents/agente-1/runs", data=form, headers={"Origin": ORIGIN})

    assert response.status_code == 200
    assert metrics.agent_errors == {"agente-1": 1}
    assert metrics.active == {"agente-1": 0}
    assert response.headers["access-control-allow-origin"] == ORIGIN  # CORS segue valendo na resposta de run
    assert "/agents/{agent_id}/runs" in _server_spans(exporter)


@pytest.mark.parametrize("stream", ["true", "false"])
def test_falha_de_run_de_team_pela_pilha_do_app(
    client: TestClient, metrics: RecordingTelemetryMetrics, stream: str
) -> None:
    response = client.post("/teams/time-1/runs", data={"message": "oi", "stream": stream})

    assert response.status_code == 200
    assert metrics.team_errors == {"time-1": 1}
    assert metrics.agent_errors == {}
    assert metrics.request_statuses == {("time-1", "error"): 1}


def test_team_inexistente_404_e_form_invalido_422_usam_unknown(
    client: TestClient, metrics: RecordingTelemetryMetrics
) -> None:
    assert client.post("/teams/inventado/runs", data={"message": "oi", "stream": "false"}).status_code == 404
    assert client.post("/teams/inventado/runs", data={"stream": "false"}).status_code == 422

    assert metrics.team_errors == {"unknown": 2}
    assert metrics.request_statuses == {("unknown", "error"): 2}


def test_preflight_head_e_get_na_rota_de_run_nao_contam(
    client: TestClient, metrics: RecordingTelemetryMetrics
) -> None:
    preflight = client.options(
        "/agents/agente-1/runs",
        headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"},
    )
    assert preflight.status_code == 200
    client.head("/agents/agente-1/runs")
    client.get("/agents/agente-1/runs")
    client.options("/teams/time-1/runs")

    assert (metrics.agent_requests, metrics.team_requests, metrics.agent_errors, metrics.active) == ({}, {}, {}, {})


def test_rotas_fora_de_run_nao_geram_metricas_de_negocio_mas_geram_span(
    client: TestClient, metrics: RecordingTelemetryMetrics, exporter: InMemorySpanExporter
) -> None:
    assert client.get("/metrics/cache").status_code in (200, 500, 503)  # depende do container; só não conta run
    client.get("/agents")
    client.post("/agents/agente-1/runs/r1/cancel")

    assert (metrics.agent_requests, metrics.team_requests, metrics.agent_errors, metrics.team_errors) == ({},) * 4
    assert metrics.active == {}
    assert "/agents" in _server_spans(exporter)


def test_sucesso_de_run_pela_pilha_nao_conta_erro(
    metrics: RecordingTelemetryMetrics, exporter: InMemorySpanExporter, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    agent = Agent(id="agente-1", name="Agente 1", model=FakeChatModel(responses=["oi"]), telemetry=False)
    factory = AppFactory()
    app = factory.create_app()
    mount_agent_os(factory, app, [agent], [])
    client = loopback_client(app, raise_server_exceptions=False)

    response = client.post("/agents/agente-1/runs", data={"message": "oi", "stream": "false"})

    assert response.status_code == 200
    assert metrics.agent_errors == {}
    assert metrics.request_statuses == {("agente-1", "success"): 1}
