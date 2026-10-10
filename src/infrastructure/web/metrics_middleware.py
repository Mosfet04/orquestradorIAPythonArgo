"""Middleware ASGI para emissão de métricas de negócio.

Intercepta a execução de agentes e teams (``POST /agents/{id}/runs``,
``POST /teams/{id}/runs``) e registra contadores, histogramas e gauges
via :class:`TelemetryMetrics`.

Implementado como middleware ASGI puro (sem BaseHTTPMiddleware) para
capturar corretamente exceções que ocorrem durante o corpo de respostas
SSE/streaming.

É a fonte única de ``agent_errors_total`` e ``team_errors_total``: cada request
de run conta no máximo um erro. O Agno 2.5.8 captura a exceção do run e o AgentOS
responde 200, então a falha também é lida no corpo da resposta:

- SSE: evento ``RunError`` (agente) ou ``TeamRunError`` (team), no formato de
  ``agno.os.utils.format_sse_event`` (``event: <nome>\\n``). No stream de team,
  ``RunError`` de membro não conta (o team pode se recuperar).
- JSON (``stream=false``): ``status == "ERROR"`` no topo do ``RunOutput``.

Além disso contam exceção que escapa da rota e status >= 400. Cancelamento
(``asyncio.CancelledError``, ex.: cliente desconectou) não conta como erro, mas,
como qualquer saída, libera ``agents_active``. Resposta 4xx usa o id ``unknown``
para não criar série por id inventado no path (o gauge ``agents_active`` ainda usa o
id do path; cardinalidade fica para a F8). ``*_requests_total`` leva
``status="error"`` quando o run falhou e ``"success"`` nos demais casos.
"""

from __future__ import annotations

import json
import re
import time
from enum import Enum

import structlog
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.infrastructure.telemetry.metrics import TelemetryMetrics

# Rotas de execução (o AgentOS também tem GET .../runs e .../runs/{run_id}/cancel|continue).
_AGENT_RUN_RE = re.compile(r"^(?:/playground)?/agents/([^/]+)/runs/?$")
_TEAM_RUN_RE = re.compile(r"^(?:/playground)?/teams/([^/]+)/runs/?$")

# Valores de agno.run.agent.RunEvent.run_error e agno.run.team.TeamRunEvent.run_error.
_AGENT_ERROR_EVENT = "RunError"
_TEAM_ERROR_EVENT = "TeamRunError"

# A resposta JSON de run só é guardada até este tamanho para ler o ``status``.
_JSON_BUFFER_LIMIT = 4 * 1024 * 1024
_UNKNOWN_ID = "unknown"

_log = structlog.get_logger(__name__)


class _Outcome(Enum):
    COMPLETED = "completed"
    RAISED = "raised"
    CANCELLED = "cancelled"


class _RunResponseInspector:
    """Observa as mensagens ASGI de resposta e diz se o run falhou."""

    def __init__(self, error_event: str) -> None:
        self.status_code: int | None = None
        self.failure: str | None = None
        self._error_event = error_event
        self._sse_marker = f"\nevent: {error_event}\n".encode()
        self._content_type = b""
        # Começa com "\n" para casar o evento na primeira linha do stream; depois guarda
        # o fim do chunk anterior para casar evento partido entre chunks.
        self._sse_tail = b"\n"
        self._json = bytearray()
        self._json_overflow = False

    def observe(self, message: Message) -> None:
        if message["type"] == "http.response.start":
            self.status_code = int(message["status"])
            if self.status_code >= 400:
                self.failure = f"status {self.status_code}"
            for name, value in message.get("headers", []):
                if name.lower() == b"content-type":
                    self._content_type = value.lower()
        elif message["type"] == "http.response.body" and self.failure is None:
            body: bytes = message.get("body", b"")
            if self._content_type.startswith(b"text/event-stream"):
                self._observe_sse(body)
            elif self._content_type.startswith(b"application/json"):
                self._observe_json(body, more_body=bool(message.get("more_body", False)))

    def _observe_sse(self, body: bytes) -> None:
        window = self._sse_tail + body
        if self._sse_marker in window:
            self.failure = f"evento {self._error_event}"
            return
        self._sse_tail = window[-(len(self._sse_marker) - 1) :]

    def _observe_json(self, body: bytes, *, more_body: bool) -> None:
        if self._json_overflow:
            return
        self._json.extend(body)
        if len(self._json) > _JSON_BUFFER_LIMIT:
            self._json_overflow = True
            self._json.clear()
            _log.warning("Resposta JSON de run grande demais para ler o status", limit_bytes=_JSON_BUFFER_LIMIT)
            return
        if more_body:
            return
        try:
            payload = json.loads(self._json)
        except (ValueError, RecursionError) as exc:  # RecursionError: aninhamento extremo
            _log.warning(
                "Resposta JSON de run inválida; status do run não verificado", error_type=type(exc).__name__
            )
            return
        if isinstance(payload, dict) and payload.get("status") == "ERROR":
            self.failure = "status ERROR no JSON"


class MetricsMiddleware:
    """Registra métricas de negócio para cada request de run de agente/team."""

    def __init__(self, app: ASGIApp, metrics: TelemetryMetrics | None = None) -> None:
        self.app = app
        self._metrics = metrics if metrics is not None else TelemetryMetrics()

    @staticmethod
    def _match_entity(path: str) -> tuple[str, bool] | None:
        """Retorna (entity_id, is_agent) ou None se não for run de agente/team."""
        agent_match = _AGENT_RUN_RE.match(path)
        if agent_match:
            return agent_match.group(1), True
        team_match = _TEAM_RUN_RE.match(path)
        if team_match:
            return team_match.group(1), False
        return None

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        match = None
        if scope["type"] == "http" and scope.get("method") == "POST":
            match = self._match_entity(scope.get("path", ""))
        if match is None:
            await self.app(scope, receive, send)
            return

        entity_id, is_agent = match
        inspector = _RunResponseInspector(_AGENT_ERROR_EVENT if is_agent else _TEAM_ERROR_EVENT)

        async def send_wrapper(message: Message) -> None:
            inspector.observe(message)
            await send(message)

        if is_agent:
            self._metrics.record_agent_active(1, entity_id)
        start = time.perf_counter()
        outcome = _Outcome.CANCELLED  # vale se sair por BaseException (CancelledError)
        try:
            await self.app(scope, receive, send_wrapper)
            outcome = _Outcome.COMPLETED
        except Exception as exc:
            outcome = _Outcome.RAISED
            _log.error(
                "Exceção durante execução de run",
                entity_type="agent" if is_agent else "team",
                entity_id=entity_id,
                status_code=inspector.status_code,
                error_type=type(exc).__name__,
            )
            raise
        finally:
            self._record_end(entity_id, is_agent, inspector, outcome, time.perf_counter() - start)

    def _record_end(
        self,
        entity_id: str,
        is_agent: bool,
        inspector: _RunResponseInspector,
        outcome: _Outcome,
        elapsed: float,
    ) -> None:
        """Registra o fim do request: uma requisição e no máximo um erro."""
        # 4xx = request rejeitado antes de executar o run (422 do FastAPI, 404 do AgentOS...):
        # o id do path pode ser inventado, então não vira série.
        status_code = inspector.status_code
        metric_id = _UNKNOWN_ID if status_code is not None and 400 <= status_code < 500 else entity_id
        failed = outcome is _Outcome.RAISED or (outcome is _Outcome.COMPLETED and inspector.failure is not None)
        request_status = "error" if failed else "success"
        if outcome is _Outcome.COMPLETED and inspector.failure is not None:
            _log.error(
                "Run finalizado com erro",
                entity_type="agent" if is_agent else "team",
                entity_id=metric_id,
                status_code=inspector.status_code,
                reason=inspector.failure,
            )

        if not is_agent:
            self._metrics.record_team_request(metric_id, request_status)
            if failed:
                self._metrics.record_team_error(metric_id)
            return

        self._metrics.record_agent_request(metric_id, request_status)
        if failed:
            self._metrics.record_agent_error(metric_id)
        self._metrics.record_agent_duration(metric_id, elapsed)
        # O gauge usa o id do path: o +1 foi registrado antes de saber o status.
        self._metrics.record_agent_active(-1, entity_id)
