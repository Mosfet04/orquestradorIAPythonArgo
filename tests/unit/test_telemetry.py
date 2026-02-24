"""Testes para o módulo de telemetria OpenTelemetry."""

from __future__ import annotations

import pytest

from unittest.mock import MagicMock, patch

from src.infrastructure.telemetry.metrics import TelemetryMetrics
from src.infrastructure.telemetry.otel_setup import (
    _build_resource,
    setup_telemetry,
    shutdown_telemetry,
)


# ── _build_resource ─────────────────────────────────────────────────


class TestBuildResource:
    def test_resource_has_service_name(self):
        config = MagicMock()
        config.otel_service_name = "test-service"
        resource = _build_resource(config)
        attrs = dict(resource.attributes)
        assert attrs["service.name"] == "test-service"

    def test_resource_has_version(self):
        config = MagicMock()
        config.otel_service_name = "test-service"
        resource = _build_resource(config)
        attrs = dict(resource.attributes)
        assert "service.version" in attrs


# ── setup_telemetry ─────────────────────────────────────────────────


class TestSetupTelemetry:
    def test_setup_disabled(self):
        """Se otel_enabled=False, não deve configurar providers."""
        config = MagicMock()
        config.otel_enabled = False
        # Não deve lançar exceção
        setup_telemetry(config)

    @patch("src.infrastructure.telemetry.otel_setup._instrument_frameworks")
    @patch("src.infrastructure.telemetry.otel_setup._setup_log_export")
    @patch("src.infrastructure.telemetry.otel_setup._setup_metrics")
    @patch("src.infrastructure.telemetry.otel_setup._setup_tracing")
    def test_setup_enabled_calls_subsystems(
        self, mock_tracing, mock_metrics, mock_logs, mock_instruments
    ):
        """Se otel_enabled=True, deve configurar todos os subsistemas."""
        config = MagicMock()
        config.otel_enabled = True
        config.otel_exporter_endpoint = "http://localhost:4317"
        config.otel_service_name = "test-svc"

        setup_telemetry(config)

        mock_tracing.assert_called_once()
        mock_metrics.assert_called_once()
        mock_logs.assert_called_once()
        mock_instruments.assert_called_once()


# ── shutdown_telemetry ──────────────────────────────────────────────


class TestShutdownTelemetry:
    @patch("src.infrastructure.telemetry.otel_setup._meter_provider", None)
    @patch("src.infrastructure.telemetry.otel_setup._tracer_provider", None)
    def test_shutdown_no_providers(self):
        """Sem providers ativos, shutdown não lança exceção."""
        shutdown_telemetry()

    def test_shutdown_with_provider(self):
        """Com provider ativo, deve chamar force_flush e shutdown."""
        import src.infrastructure.telemetry.otel_setup as mod

        mock_tp = MagicMock()
        original_tp = mod._tracer_provider
        original_mp = mod._meter_provider
        try:
            mod._tracer_provider = mock_tp
            mod._meter_provider = None
            shutdown_telemetry()
            mock_tp.force_flush.assert_called_once()
            mock_tp.shutdown.assert_called_once()
        finally:
            mod._tracer_provider = original_tp
            mod._meter_provider = original_mp


# ── TelemetryMetrics ────────────────────────────────────────────────


class TestTelemetryMetrics:
    @pytest.fixture(autouse=True)
    def _patch_instruments(self, monkeypatch):
        # replace all global instruments with simple mocks to avoid OTel complexity
        from src.infrastructure.telemetry import metrics as _metrics

        names = [
            "agent_requests_total",
            "agent_response_duration",
            "team_requests_total",
            "agents_loaded",
            "teams_loaded",
            "cache_hits_total",
            "cache_misses_total",
            "tool_calls_total",
            "tool_call_duration",
            "tool_call_errors_total",
            "startup_duration",
        ]
        for n in names:
            m = MagicMock()
            # instrument may be counter or histogram; ensure both methods exist
            m.add = MagicMock()
            m.record = MagicMock()
            monkeypatch.setattr(_metrics, n, m)
        yield

    def test_record_agent_request_no_error(self):
        """Registrar requisição de agente não deve lançar exceção."""
        TelemetryMetrics.record_agent_request("agent-1", "success")

    def test_record_agent_duration_no_error(self):
        TelemetryMetrics.record_agent_duration("agent-1", 1.5)

    def test_record_team_request_no_error(self):
        TelemetryMetrics.record_team_request("team-1", "success")

    def test_record_cache_hit_no_error(self):
        TelemetryMetrics.record_cache_hit("agents")

    def test_record_cache_miss_no_error(self):
        TelemetryMetrics.record_cache_miss("agents")

    def test_record_tool_call_no_error(self):
        TelemetryMetrics.record_tool_call("tool-1", 0.5, "success")

    def test_record_tool_call_error_status(self):
        TelemetryMetrics.record_tool_call("tool-1", 0.5, "error")

    def test_record_startup_duration_no_error(self):
        TelemetryMetrics.record_startup_duration(2.5)

    def test_record_agents_loaded(self):
        TelemetryMetrics.record_agents_loaded(3)

    def test_record_teams_loaded(self):
        TelemetryMetrics.record_teams_loaded(1)

    def test_measure_duration_context_manager(self):
        """Context manager deve medir duração sem erro."""
        with TelemetryMetrics.measure_duration("agent-1"):
            pass  # simula operação
