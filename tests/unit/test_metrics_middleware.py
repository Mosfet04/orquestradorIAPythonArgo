"""Testes para o middleware de métricas de negócio."""

from __future__ import annotations

import pytest
from unittest.mock import patch

from starlette.testclient import TestClient
from fastapi import FastAPI

from src.infrastructure.web.metrics_middleware import MetricsMiddleware


@pytest.fixture()
def app_with_middleware():
    """Cria app FastAPI mínima com MetricsMiddleware para testes."""
    app = FastAPI()
    app.add_middleware(MetricsMiddleware)

    @app.post("/agents/{agent_id}/runs")
    async def agent_run(agent_id: str):
        return {"agent_id": agent_id, "status": "ok"}

    @app.post("/teams/{team_id}/runs")
    async def team_run(team_id: str):
        return {"team_id": team_id, "status": "ok"}

    @app.get("/admin/health")
    async def health():
        return {"status": "healthy"}

    @app.post("/agents/{agent_id}/runs/error")
    async def agent_run_error(agent_id: str):
        raise ValueError("Erro simulado")

    return app


@pytest.fixture()
def client(app_with_middleware):
    return TestClient(app_with_middleware, raise_server_exceptions=False)


class TestMetricsMiddleware:
    """Testes do MetricsMiddleware."""

    @patch("src.infrastructure.web.metrics_middleware.TelemetryMetrics")
    def test_agent_run_records_metrics(self, mock_metrics, client):
        """Request de agente deve registrar request, active e duration."""
        response = client.post("/agents/agent-1/runs")
        assert response.status_code == 200

        mock_metrics.record_agent_request.assert_called_once_with("agent-1")
        mock_metrics.record_agent_active.assert_any_call(1, "agent-1")
        mock_metrics.record_agent_active.assert_any_call(-1, "agent-1")
        mock_metrics.record_agent_duration.assert_called_once()

    @patch("src.infrastructure.web.metrics_middleware.TelemetryMetrics")
    def test_team_run_records_metrics(self, mock_metrics, client):
        """Request de team deve registrar request de team."""
        response = client.post("/teams/team-1/runs")
        assert response.status_code == 200

        mock_metrics.record_team_request.assert_called_once_with("team-1")
        # Não deve registrar métricas de agente
        mock_metrics.record_agent_request.assert_not_called()

    @patch("src.infrastructure.web.metrics_middleware.TelemetryMetrics")
    def test_non_agent_request_no_metrics(self, mock_metrics, client):
        """Requests que não são de agente/team não devem registrar métricas."""
        response = client.get("/admin/health")
        assert response.status_code == 200

        mock_metrics.record_agent_request.assert_not_called()
        mock_metrics.record_team_request.assert_not_called()

    @patch("src.infrastructure.web.metrics_middleware.TelemetryMetrics")
    def test_agent_error_records_error_metric(self, mock_metrics, client):
        """Request com erro 500 deve registrar agent_errors_total."""
        response = client.post("/agents/agent-1/runs/error")
        assert response.status_code == 500

        mock_metrics.record_agent_error.assert_called_once_with("agent-1")
        # Deve decrementar active mesmo com erro
        mock_metrics.record_agent_active.assert_any_call(-1, "agent-1")
