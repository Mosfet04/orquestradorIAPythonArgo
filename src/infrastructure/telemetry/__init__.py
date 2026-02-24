"""
Módulo de telemetria OpenTelemetry para o Orquestrador IA.

Exporta traces, métricas e logs via OTLP para Grafana LGTM
(Loki + Grafana + Tempo + Mimir).
"""

from .otel_setup import setup_telemetry, shutdown_telemetry
from .metrics import TelemetryMetrics

__all__ = [
    "setup_telemetry",
    "shutdown_telemetry",
    "TelemetryMetrics",
]
