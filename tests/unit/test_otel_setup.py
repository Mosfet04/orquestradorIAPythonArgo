import sys
import types
import logging

from src.infrastructure.telemetry import otel_setup


def test__build_resource_fields():
    class DummyCfg:
        otel_service_name = "svc"

    res = otel_setup._build_resource(DummyCfg())
    attrs = res.attributes
    assert attrs["service.name"] == "svc"
    assert "deployment.environment" in attrs
    assert "service.namespace" in attrs


def test__setup_tracing_and_metrics(monkeypatch):
    class DummyExporter:
        def __init__(self, **kwargs):
            pass

        def shutdown(self):
            pass

    class DummyReader:
        def __init__(self, *a, **k):
            pass

        def _instrument_class_temporality(self, *a, **k):
            pass

        @property
        def _instrument_class_aggregation(self):
            return None

        def _set_collect_callback(self, *a, **k):
            pass

    monkeypatch.setattr(otel_setup, "OTLPSpanExporter", DummyExporter)
    monkeypatch.setattr(otel_setup, "OTLPMetricExporter", DummyExporter)
    monkeypatch.setattr(otel_setup, "PeriodicExportingMetricReader", DummyReader)

    class DummyResource:
        pass

    # Tracing
    tp = otel_setup._setup_tracing(DummyResource(), "http://x")
    assert tp is not None
    # Metrics
    mp = otel_setup._setup_metrics(DummyResource(), "http://x")
    assert mp is not None


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
