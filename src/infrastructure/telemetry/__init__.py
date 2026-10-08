"""
Módulo de telemetria OpenTelemetry para o Orquestrador IA.

Exporta traces, métricas e logs via OTLP para Grafana LGTM
(Loki + Grafana + Tempo + Mimir).
"""

from .metrics import TelemetryMetrics
from .otel_setup import setup_telemetry, shutdown_telemetry

__all__ = [
    "TelemetryMetrics",
    "setup_telemetry",
    "shutdown_telemetry",
]
