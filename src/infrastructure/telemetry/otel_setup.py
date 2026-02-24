"""
Configuração centralizada do OpenTelemetry SDK.

Inicializa TracerProvider, MeterProvider e LoggerProvider com exporters
OTLP gRPC apontando para o Grafana LGTM (via OTEL Collector embutido).

Traces  → Grafana Tempo
Métricas → Grafana Mimir
Logs    → Grafana Loki
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING

from opentelemetry import trace, metrics
from opentelemetry.sdk.resources import Resource, SERVICE_NAME, SERVICE_VERSION
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.grpc.metric_exporter import OTLPMetricExporter

if TYPE_CHECKING:
    from src.infrastructure.config.app_config import AppConfig

logger = logging.getLogger(__name__)

# ── Globals para shutdown controlado ────────────────────────────────
_tracer_provider: TracerProvider | None = None
_meter_provider: MeterProvider | None = None


def _build_resource(config: AppConfig) -> Resource:
    """Cria o Resource OTel com atributos do serviço."""
    return Resource.create(
        {
            SERVICE_NAME: config.otel_service_name,
            SERVICE_VERSION: "2.0.0",
            "deployment.environment": os.getenv("ENVIRONMENT", "development"),
            "service.namespace": "orquestrador-ia",
            "host.name": os.getenv("HOSTNAME", "localhost"),
        }
    )


def _setup_tracing(resource: Resource, endpoint: str) -> TracerProvider:
    """Configura TracerProvider + BatchSpanProcessor → OTLP gRPC."""
    exporter = OTLPSpanExporter(
        endpoint=endpoint,
        insecure=True,
    )
    provider = TracerProvider(resource=resource)
    provider.add_span_processor(
        BatchSpanProcessor(
            exporter,
            max_queue_size=2048,
            max_export_batch_size=512,
            schedule_delay_millis=5000,
        )
    )
    trace.set_tracer_provider(provider)
    return provider


def _setup_metrics(resource: Resource, endpoint: str) -> MeterProvider:
    """Configura MeterProvider + PeriodicExportingMetricReader → OTLP gRPC."""
    exporter = OTLPMetricExporter(
        endpoint=endpoint,
        insecure=True,
    )
    reader = PeriodicExportingMetricReader(
        exporter,
        export_interval_millis=30_000,
    )
    provider = MeterProvider(resource=resource, metric_readers=[reader])
    metrics.set_meter_provider(provider)
    return provider


def _instrument_frameworks() -> None:
    """Aplica auto-instrumentação em frameworks usados pela app.

    - FastAPI: rastreia toda requisição HTTP recebida
    - HTTPX: rastreia chamadas HTTP saintes (tools, APIs externas)
    - Agno: captura spans de execução de agentes/teams
    """
    try:
        from opentelemetry.instrumentation.httpx import HTTPXClientInstrumentor

        HTTPXClientInstrumentor().instrument()
        logger.info("HTTPX instrumentado com sucesso")
    except Exception as exc:
        logger.warning("Falha ao instrumentar HTTPX: %s", exc)

    try:
        from openinference.instrumentation.agno import AgnoInstrumentor

        AgnoInstrumentor().instrument()
        logger.info("Agno instrumentado com sucesso")
    except Exception as exc:
        logger.warning("Falha ao instrumentar Agno: %s", exc)


def _setup_log_export(resource: Resource, endpoint: str) -> None:
    """Configura exportação de logs via OTLP para Loki.

    Usa o LoggingHandler do OTel SDK para capturar logs do stdlib
    (structlog faz output via stdlib) e enviá-los via OTLP gRPC.
    """
    try:
        from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
        from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
        from opentelemetry.exporter.otlp.proto.grpc._log_exporter import (
            OTLPLogExporter,
        )
        from opentelemetry._logs import set_logger_provider

        log_exporter = OTLPLogExporter(endpoint=endpoint, insecure=True)
        log_provider = LoggerProvider(resource=resource)
        log_provider.add_log_record_processor(
            BatchLogRecordProcessor(log_exporter)
        )
        set_logger_provider(log_provider)

        # Adiciona handler ao root logger — captura tudo do stdlib/structlog
        handler = LoggingHandler(
            level=logging.INFO,
            logger_provider=log_provider,
        )
        logging.getLogger().addHandler(handler)
        logger.info("Log export OTLP configurado com sucesso")
    except Exception as exc:
        logger.warning("Falha ao configurar log export OTLP: %s", exc)


def setup_telemetry(config: AppConfig) -> None:
    """Inicializa toda a stack de telemetria OpenTelemetry.

    Deve ser chamada **antes** da criação de agentes e do mount do AgentOS,
    para que todos os spans gerados pelo agno/FastAPI sejam capturados.

    Args:
        config: Configuração da aplicação com endpoint OTLP.
    """
    global _tracer_provider, _meter_provider

    if not config.otel_enabled:
        logger.info("Telemetria OpenTelemetry desabilitada (OTEL_ENABLED=false)")
        return

    endpoint = config.otel_exporter_endpoint
    logger.info("Inicializando OpenTelemetry → endpoint=%s", endpoint)

    resource = _build_resource(config)

    _tracer_provider = _setup_tracing(resource, endpoint)
    logger.info("TracerProvider configurado (traces → Tempo)")

    _meter_provider = _setup_metrics(resource, endpoint)
    logger.info("MeterProvider configurado (métricas → Mimir)")

    _setup_log_export(resource, endpoint)
    logger.info("LoggerProvider configurado (logs → Loki)")

    _instrument_frameworks()
    logger.info("OpenTelemetry inicializado com sucesso")


def shutdown_telemetry() -> None:
    """Faz flush e shutdown dos providers para garantir envio pendente."""
    global _tracer_provider, _meter_provider

    if _tracer_provider:
        try:
            _tracer_provider.force_flush(timeout_millis=5000)
            _tracer_provider.shutdown()
            logger.info("TracerProvider encerrado")
        except Exception as exc:
            logger.warning("Erro no shutdown do TracerProvider: %s", exc)
        _tracer_provider = None

    if _meter_provider:
        try:
            _meter_provider.force_flush(timeout_millis=5000)
            _meter_provider.shutdown()
            logger.info("MeterProvider encerrado")
        except Exception as exc:
            logger.warning("Erro no shutdown do MeterProvider: %s", exc)
        _meter_provider = None
