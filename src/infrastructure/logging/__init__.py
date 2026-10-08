"""
Módulo de logging seguro para o Orquestrador IA usando structlog.
"""

from .decorators import log_ai_interaction, log_execution, log_http_request, log_performance
from .structlog_logger import (
    DataSanitizer,
    LogContext,
    LoggerFactory,
    LogLevel,
    StructlogLogger,
    app_logger,
    setup_structlog,
)

# Manter compatibilidade com código existente
SecureLogger = StructlogLogger

__all__ = [
    'DataSanitizer',
    'LogContext',
    'LogLevel',
    'LoggerFactory',
    'SecureLogger',  # Alias para compatibilidade
    'StructlogLogger',
    'app_logger',
    'log_ai_interaction',
    'log_execution',
    'log_http_request',
    'log_performance',
    'setup_structlog'
]
