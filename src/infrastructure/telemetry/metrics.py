"""
Métricas de negócio e sistema do Orquestrador IA.

Instrumentos registrados via OpenTelemetry Meter e exportados
automaticamente via OTLP push para Grafana Mimir.
"""

from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Generator

from opentelemetry import metrics

_meter = metrics.get_meter("orquestrador-ia", version="2.0.0")

# ── Métricas de negócio ─────────────────────────────────────────────

agent_requests_total = _meter.create_counter(
    name="agent_requests_total",
    description="Total de requisições processadas por agentes",
    unit="1",
)

agent_response_duration = _meter.create_histogram(
    name="agent_response_duration_seconds",
    description="Duração das respostas dos agentes em segundos",
    unit="s",
)

team_requests_total = _meter.create_counter(
    name="team_requests_total",
    description="Total de requisições processadas por teams",
    unit="1",
)

agents_loaded = _meter.create_up_down_counter(
    name="agents_loaded",
    description="Número de agentes ativos carregados na memória",
    unit="1",
)

teams_loaded = _meter.create_up_down_counter(
    name="teams_loaded",
    description="Número de teams ativos carregados na memória",
    unit="1",
)

# ── Métricas de cache ───────────────────────────────────────────────

cache_hits_total = _meter.create_counter(
    name="cache_hits_total",
    description="Total de cache hits",
    unit="1",
)

cache_misses_total = _meter.create_counter(
    name="cache_misses_total",
    description="Total de cache misses",
    unit="1",
)

# ── Métricas de tools HTTP ──────────────────────────────────────────

tool_calls_total = _meter.create_counter(
    name="tool_calls_total",
    description="Total de chamadas a tools HTTP externas",
    unit="1",
)

tool_call_duration = _meter.create_histogram(
    name="tool_call_duration_seconds",
    description="Duração das chamadas a tools HTTP em segundos",
    unit="s",
)

tool_call_errors_total = _meter.create_counter(
    name="tool_call_errors_total",
    description="Total de erros em chamadas a tools HTTP",
    unit="1",
)

# ── Métricas de startup ────────────────────────────────────────────

startup_duration = _meter.create_histogram(
    name="startup_duration_seconds",
    description="Duração do startup da aplicação em segundos",
    unit="s",
)


class TelemetryMetrics:
    """Facade para registrar métricas de forma simplificada."""

    @staticmethod
    def record_agent_request(agent_id: str, status: str = "success") -> None:
        """Registra uma requisição a um agente."""
        agent_requests_total.add(1, {"agent_id": agent_id, "status": status})

    @staticmethod
    def record_agent_duration(agent_id: str, duration_s: float) -> None:
        """Registra a duração de uma resposta de agente."""
        agent_response_duration.record(
            duration_s, {"agent_id": agent_id}
        )

    @staticmethod
    def record_team_request(team_id: str, status: str = "success") -> None:
        """Registra uma requisição a um team."""
        team_requests_total.add(1, {"team_id": team_id, "status": status})

    @staticmethod
    def record_agents_loaded(count: int) -> None:
        """Define o número de agentes carregados."""
        agents_loaded.add(count)

    @staticmethod
    def record_teams_loaded(count: int) -> None:
        """Define o número de teams carregados."""
        teams_loaded.add(count)

    @staticmethod
    def record_cache_hit(cache_name: str = "agents") -> None:
        """Registra um cache hit."""
        cache_hits_total.add(1, {"cache": cache_name})

    @staticmethod
    def record_cache_miss(cache_name: str = "agents") -> None:
        """Registra um cache miss."""
        cache_misses_total.add(1, {"cache": cache_name})

    @staticmethod
    def record_tool_call(
        tool_id: str, duration_s: float, status: str = "success"
    ) -> None:
        """Registra uma chamada a tool HTTP."""
        tool_calls_total.add(1, {"tool_id": tool_id, "status": status})
        tool_call_duration.record(duration_s, {"tool_id": tool_id})
        if status == "error":
            tool_call_errors_total.add(1, {"tool_id": tool_id})

    @staticmethod
    def record_startup_duration(duration_s: float) -> None:
        """Registra a duração do startup."""
        startup_duration.record(duration_s)

    @staticmethod
    @contextmanager
    def measure_duration(
        agent_id: str,
    ) -> Generator[None, None, None]:
        """Context manager para medir duração de uma operação de agente."""
        start = time.perf_counter()
        try:
            yield
        finally:
            elapsed = time.perf_counter() - start
            TelemetryMetrics.record_agent_duration(agent_id, elapsed)
