"""Falha de run com Agno real (Agent/Team + AgentOS + AgnoInstrumentor) conta exatamente um erro.

O Agno 2.5.8 captura a exceção do run (``RunOutput.status == ERROR``; no stream, evento
``RunError``/``TeamRunError``) e o AgentOS responde 200. O span do OpenInference fica OK,
então a falha só é visível na borda HTTP, pelo ``MetricsMiddleware``.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from agno.agent import Agent
from agno.os import AgentOS
from agno.team import Team
from fastapi import FastAPI
from openinference.instrumentation.agno import AgnoInstrumentor
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.trace import StatusCode
from starlette.testclient import TestClient

from src.infrastructure.web.metrics_middleware import MetricsMiddleware
from tests.fakes import FakeChatModel, RecordingTelemetryMetrics


@pytest.fixture
def exporter() -> Iterator[InMemorySpanExporter]:
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))
    instrumentor = AgnoInstrumentor()
    instrumentor.instrument(tracer_provider=provider)
    try:
        yield exporter
    finally:
        instrumentor.uninstrument()
        provider.shutdown()


@pytest.fixture
def metrics() -> RecordingTelemetryMetrics:
    return RecordingTelemetryMetrics()


def _client(metrics: RecordingTelemetryMetrics, responses: list[str]) -> TestClient:
    """App como a do AppFactory: base FastAPI com MetricsMiddleware e AgentOS montado nela."""
    agent = Agent(id="agente-1", name="Agente 1", model=FakeChatModel(responses=list(responses)), telemetry=False)
    member = Agent(id="membro-1", name="Membro", model=FakeChatModel(responses=[]), telemetry=False)
    team = Team(
        id="time-1",
        name="Time 1",
        members=[member],
        model=FakeChatModel(responses=list(responses)),
        telemetry=False,
    )
    base = FastAPI()
    base.add_middleware(MetricsMiddleware, metrics=metrics)
    agent_os = AgentOS(
        agents=[agent],
        teams=[team],
        base_app=base,
        on_route_conflict="preserve_base_app",
        tracing=False,
        telemetry=False,
    )
    return TestClient(agent_os.get_app(), raise_server_exceptions=False)


@pytest.mark.parametrize("stream", ["true", "false"], ids=["stream", "nao-stream"])
def test_falha_de_run_de_agente_conta_um_erro(
    metrics: RecordingTelemetryMetrics, exporter: InMemorySpanExporter, stream: str
) -> None:
    client = _client(metrics, responses=[])  # roteiro vazio: o modelo falha na 1a chamada

    response = client.post("/agents/agente-1/runs", data={"message": "oi", "stream": stream})

    assert response.status_code == 200  # o AgentOS não sinaliza a falha no status
    assert metrics.agent_errors == {"agente-1": 1}
    assert metrics.request_statuses == {("agente-1", "error"): 1}
    assert metrics.active == {"agente-1": 0}
    agent_spans = [s for s in exporter.get_finished_spans() if (s.attributes or {}).get("agno.agent.id")]
    assert agent_spans, "o AgnoInstrumentor deveria ter emitido o span do run"
    assert all(s.status.status_code is not StatusCode.ERROR for s in agent_spans)


@pytest.mark.parametrize("stream", ["true", "false"], ids=["stream", "nao-stream"])
def test_falha_de_run_de_team_conta_um_erro(
    metrics: RecordingTelemetryMetrics, exporter: InMemorySpanExporter, stream: str
) -> None:
    client = _client(metrics, responses=[])

    response = client.post("/teams/time-1/runs", data={"message": "oi", "stream": stream})

    assert response.status_code == 200
    assert metrics.team_errors == {"time-1": 1}
    assert metrics.request_statuses == {("time-1", "error"): 1}
    assert metrics.agent_errors == {}


@pytest.mark.parametrize("stream", ["true", "false"], ids=["stream", "nao-stream"])
@pytest.mark.parametrize("path", ["/agents/agente-1/runs", "/teams/time-1/runs"], ids=["agente", "team"])
def test_run_com_sucesso_nao_conta_erro(
    metrics: RecordingTelemetryMetrics, exporter: InMemorySpanExporter, stream: str, path: str
) -> None:
    client = _client(metrics, responses=["olá"])

    response = client.post(path, data={"message": "oi", "stream": stream})

    assert response.status_code == 200
    assert metrics.agent_errors == {}
    assert metrics.team_errors == {}
    assert set(metrics.request_statuses) == {(path.split("/")[2], "success")}


def test_agente_inexistente_conta_como_unknown(metrics: RecordingTelemetryMetrics) -> None:
    client = _client(metrics, responses=[])

    response = client.post("/agents/id-inventado/runs", data={"message": "oi", "stream": "false"})

    assert response.status_code == 404
    assert metrics.request_statuses == {("unknown", "error"): 1}
    assert metrics.agent_errors == {"unknown": 1}


def test_form_invalido_422_conta_como_unknown(metrics: RecordingTelemetryMetrics) -> None:
    client = _client(metrics, responses=[])

    response = client.post("/agents/id-inventado/runs", data={"stream": "false"})  # sem `message`

    assert response.status_code == 422
    assert metrics.request_statuses == {("unknown", "error"): 1}
    assert metrics.agent_errors == {"unknown": 1}
