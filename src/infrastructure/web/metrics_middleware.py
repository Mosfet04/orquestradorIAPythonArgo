"""Middleware ASGI para emissão de métricas de negócio.

Intercepta requisições de agentes e teams (``/agents/*/runs``,
``/teams/*/runs``) e registra contadores, histogramas e gauges
via :class:`TelemetryMetrics`.

Posicionamento na stack de middlewares:
    CORS → MetricsMiddleware → PlaygroundPrefixMiddleware → rotas

Dessa forma **todo** request de agente/team é instrumentado,
independentemente de vir da AGUI, do Playground ou de chamada direta.
"""

from __future__ import annotations

import re
import time

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import Response

from src.infrastructure.telemetry.metrics import TelemetryMetrics

# Padrões de URL que representam execução de agentes/teams
_AGENT_RUN_RE = re.compile(r"^(?:/playground)?/agents/([^/]+)/runs")
_TEAM_RUN_RE = re.compile(r"^(?:/playground)?/teams/([^/]+)/runs")


class MetricsMiddleware(BaseHTTPMiddleware):
    """Registra métricas de negócio para cada request de agente/team."""

    @staticmethod
    def _match_entity(path: str):
        """Retorna (entity_id, is_agent) ou None se não for agente/team."""
        agent_match = _AGENT_RUN_RE.match(path)
        if agent_match:
            return agent_match.group(1), True
        team_match = _TEAM_RUN_RE.match(path)
        if team_match:
            return team_match.group(1), False
        return None

    @staticmethod
    def _record_start_metrics(entity_id: str, is_agent: bool):
        """Registra métricas de início de request."""
        if is_agent:
            TelemetryMetrics.record_agent_request(entity_id)
            TelemetryMetrics.record_agent_active(1, entity_id)
        else:
            TelemetryMetrics.record_team_request(entity_id)

    @staticmethod
    def _record_end_metrics(entity_id: str, is_agent: bool, elapsed: float, error: bool = False):
        """Registra métricas de fim de request."""
        if not is_agent:
            return
        if error:
            TelemetryMetrics.record_agent_error(entity_id)
        TelemetryMetrics.record_agent_duration(entity_id, elapsed)
        TelemetryMetrics.record_agent_active(-1, entity_id)

    async def dispatch(self, request: Request, call_next) -> Response:
        path: str = request.scope.get("path", "")

        match = self._match_entity(path)
        if match is None:
            return await call_next(request)

        entity_id, is_agent = match
        self._record_start_metrics(entity_id, is_agent)

        start = time.perf_counter()
        try:
            response = await call_next(request)
            elapsed = time.perf_counter() - start
            self._record_end_metrics(entity_id, is_agent, elapsed, error=response.status_code >= 400)
            return response
        except Exception:
            elapsed = time.perf_counter() - start
            self._record_end_metrics(entity_id, is_agent, elapsed, error=True)
            raise
