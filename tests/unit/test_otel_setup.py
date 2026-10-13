import logging
import sys
import types

import pytest
from opentelemetry import metrics, trace
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from src.infrastructure.telemetry import otel_setup


def test__build_resource_fields():
    class DummyCfg:
        otel_service_name = "svc"

    res = otel_setup._build_resource(DummyCfg())
    attrs = res.attributes
    assert attrs["service.name"] == "svc"
    assert "deployment.environment" in attrs
    assert "service.namespace" in attrs


@pytest.mark.usefixtures("reset_otel_providers")
def test__setup_tracing_and_metrics(monkeypatch):
    """Providers viram globais e exportam pelo exporter/reader configurados (em memória, sem rede)."""
    span_exporter = InMemorySpanExporter()
    metric_reader = InMemoryMetricReader()
    created = {}

    def fake_span_exporter(**kwargs):
        created["span_exporter"] = kwargs
        return span_exporter

    def fake_metric_exporter(**kwargs):
        created["metric_exporter"] = kwargs
        return object()

    def fake_periodic_reader(exporter, **kwargs):
        created["reader"] = kwargs
        return metric_reader

    monkeypatch.setattr(otel_setup, "OTLPSpanExporter", fake_span_exporter)
    monkeypatch.setattr(otel_setup, "OTLPMetricExporter", fake_metric_exporter)
    monkeypatch.setattr(otel_setup, "PeriodicExportingMetricReader", fake_periodic_reader)

    class Cfg:
        otel_service_name = "svc-teste"

    resource = otel_setup._build_resource(Cfg())

    tp = otel_setup._setup_tracing(resource, "http://x")
    mp = otel_setup._setup_metrics(resource, "http://x")

    assert trace.get_tracer_provider() is tp
    assert metrics.get_meter_provider() is mp
    assert created["span_exporter"] == {"endpoint": "http://x", "insecure": True}
    assert created["metric_exporter"] == {"endpoint": "http://x", "insecure": True}
    assert created["reader"] == {"export_interval_millis": 30_000}

    with trace.get_tracer("teste").start_as_current_span("operacao"):
        pass
    tp.force_flush()
    (span,) = span_exporter.get_finished_spans()
    assert span.name == "operacao"
    assert span.resource.attributes["service.name"] == "svc-teste"

    metrics.get_meter("teste").create_counter("chamadas").add(2)
    data = metric_reader.get_metrics_data()
    points = [
        point.value
        for rm in data.resource_metrics
        for sm in rm.scope_metrics
        for metric in sm.metrics
        if metric.name == "chamadas"
        for point in metric.data.data_points
    ]
    assert points == [2]


def test__instrument_frameworks_all_fail(monkeypatch, caplog):
    monkeypatch.setitem(
        sys.modules, "opentelemetry.instrumentation.httpx", types.ModuleType("fail")
    )
    monkeypatch.setitem(
        sys.modules, "openinference.instrumentation.agno", types.ModuleType("fail")
    )
    caplog.set_level(logging.WARNING)
    otel_setup._instrument_frameworks()
    msgs = [r.message for r in caplog.records]
    assert any("Falha ao instrumentar" in m for m in msgs)


def test__setup_log_export_handles_exception(monkeypatch, caplog):
    monkeypatch.setattr(otel_setup, "logging", logging)
    monkeypatch.setitem(
        sys.modules, "opentelemetry.sdk._logs", types.ModuleType("fail")
    )
    caplog.set_level(logging.WARNING)
    otel_setup._setup_log_export(object(), "http://x")
    msgs = [r.message for r in caplog.records]
    assert any("Falha ao configurar log export OTLP" in m for m in msgs)


class DummyConfig:
    def __init__(self, enabled: bool = False):
        self.otel_enabled = enabled
        self.otel_exporter_endpoint = "http://localhost:4317"
        self.otel_service_name = "orquestrador-ia"


def test_setup_telemetry_disabled_does_not_raise():
    cfg = DummyConfig(enabled=False)
    # Deve retornar sem exceção
    otel_setup.setup_telemetry(cfg)


def test__instrument_frameworks_nao_instrumenta_fastapi_globalmente(monkeypatch):
    """A instrumentação do FastAPI é só por app (AppFactory._instrument_fastapi), nunca global."""
    import fastapi
    from fastapi.applications import FastAPI as fastapi_original
    from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

    instrumentor = FastAPIInstrumentor()  # singleton do BaseInstrumentor
    if instrumentor.is_instrumented_by_opentelemetry:
        instrumentor.uninstrument()
    monkeypatch.setitem(
        sys.modules, "opentelemetry.instrumentation.httpx", types.ModuleType("fail")
    )
    monkeypatch.setitem(
        sys.modules, "openinference.instrumentation.agno", types.ModuleType("fail")
    )

    try:
        otel_setup._instrument_frameworks()
        assert fastapi.FastAPI is fastapi_original
    finally:
        if instrumentor.is_instrumented_by_opentelemetry:
            instrumentor.uninstrument()
