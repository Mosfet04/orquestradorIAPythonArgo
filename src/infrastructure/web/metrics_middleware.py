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

    async def dispatch(self, request: Request, call_next) -> Response:
        path: str = request.scope.get("path", "")

        agent_match = _AGENT_RUN_RE.match(path)
        team_match = _TEAM_RUN_RE.match(path) if not agent_match else None

        # Requests que não são de agente/team passam direto sem overhead
        if not agent_match and not team_match:
            return await call_next(request)

        entity_id: str
        is_agent: bool

        if agent_match:
            entity_id = agent_match.group(1)
            is_agent = True
        else:
            assert team_match is not None  # noqa: S101 — logicamente garantido
            entity_id = team_match.group(1)
            is_agent = False

        if is_agent:
            TelemetryMetrics.record_agent_request(entity_id)
            TelemetryMetrics.record_agent_active(1, entity_id)
        else:
            TelemetryMetrics.record_team_request(entity_id)

        start = time.perf_counter()
        try:
            response = await call_next(request)

            if response.status_code >= 400 and is_agent:
                TelemetryMetrics.record_agent_error(entity_id)

            return response
        except Exception:
            if is_agent:
                TelemetryMetrics.record_agent_error(entity_id)
            raise
        finally:
            elapsed = time.perf_counter() - start
            if is_agent:
                TelemetryMetrics.record_agent_duration(entity_id, elapsed)
                TelemetryMetrics.record_agent_active(-1, entity_id)
