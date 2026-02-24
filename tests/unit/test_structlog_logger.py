import logging
import sys
import types
from types import SimpleNamespace


from src.infrastructure.logging.structlog_logger import (
    DataSanitizer,
    add_correlation_id,
    add_otel_trace_context,
    sanitize_log_data,
    setup_structlog,
    LoggerFactory,
)


def test_data_sanitizer_masks_api_key():
    s = "user=alice api_key=abcdEFGHijklMNOPqrstUVWX"
    out = DataSanitizer.sanitize_data(s)
    assert "API_KEY_MASKED" in out.upper()


def test_add_correlation_id_creates_short_id():
    ev = {}
    res = add_correlation_id(None, None, ev)
    assert "correlation_id" in res
    assert len(res["correlation_id"]) == 8


def test_add_otel_trace_context_with_span(monkeypatch):
    # Simula um span com contexto contendo trace_id/span_id
    class DummySpan:
        def get_span_context(self):
            return SimpleNamespace(trace_id=1, span_id=2)

    trace_mod = types.ModuleType("opentelemetry.trace")
    trace_mod.get_current_span = staticmethod(DummySpan)
    pkg = types.ModuleType("opentelemetry")
    pkg.trace = trace_mod

    # Injetar módulos fake no sys.modules
    monkeypatch.setitem(sys.modules, "opentelemetry", pkg)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", trace_mod)

    ev = {}
    res = add_otel_trace_context(None, "info", ev)
    assert "trace_id" in res and "span_id" in res
    assert len(res["trace_id"]) == 32
    assert len(res["span_id"]) == 16


def test_add_otel_trace_context_logs_on_exception(monkeypatch, caplog):
    # Faz get_current_span levantar exceção
    trace_mod = types.ModuleType("opentelemetry.trace")

    def bad_span():
        raise RuntimeError("no otel")
    
    trace_mod.get_current_span = staticmethod(bad_span)
    pkg = types.ModuleType("opentelemetry")
    pkg.trace = trace_mod
    monkeypatch.setitem(sys.modules, "opentelemetry", pkg)
    monkeypatch.setitem(sys.modules, "opentelemetry.trace", trace_mod)

    caplog.set_level(logging.DEBUG)
    ev = {}
    res = add_otel_trace_context(None, "info", ev)
    assert res == ev
    # Deve ter logado em DEBUG
    assert any("Falha ao injetar contexto OTel" in r.message or "Falha ao injetar" in r.message for r in caplog.records)


def test_sanitize_log_data_masks_password():
    ev = {"password": "secret", "timestamp": "ts"}
    out = sanitize_log_data(None, None, ev.copy())
    assert out["password"].startswith("***MASKED***")
    assert out["timestamp"] == "ts"


def test_setup_structlog_and_logger_factory():
    # Não deve levantar
    setup_structlog()
    logger = LoggerFactory.get_logger("unit-test")
    assert logger is not None
from src.infrastructure.logging.structlog_logger import LoggerFactory, StructlogLogger, DataSanitizer


def test_structlog_logger_basic_flow(monkeypatch):
    # Forçar ambiente de dev
    monkeypatch.setenv('ENVIRONMENT', 'development')
    logger = LoggerFactory.get_logger("test_structlog")
    assert isinstance(logger, StructlogLogger)

    # Bind e contexto
    logger.set_context(user_id="u1", request_id="r1")
    logger.info("msg", action="do")
    logger.performance("perf", duration=1.23)
    logger.security("sec", ip="127.0.0.1")

    # Sanitização simples
    out = DataSanitizer.sanitize_data({"password": "abc12345"})
    assert '***' in out['password']
