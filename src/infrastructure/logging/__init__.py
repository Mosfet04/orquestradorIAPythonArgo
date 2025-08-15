"""
Módulo de logging seguro para o Orquestrador IA usando structlog.
"""

from .structlog_logger import (
    StructlogLogger, 
    LoggerFactory, 
    DataSanitizer, 
    LogLevel, 
    LogContext,
    app_logger,
    setup_structlog
)

from .decorators import (
    log_execution,
    log_performance,
    log_ai_interaction,
    log_http_request
)

# Manter compatibilidade com código existente
SecureLogger = StructlogLogger

__all__ = [
    'StructlogLogger',
    'SecureLogger',  # Alias para compatibilidade
    'LoggerFactory', 
    'DataSanitizer',
    'LogLevel',
    'LogContext',
    'app_logger',
    'setup_structlog',
    'log_execution',
    'log_performance',
    'log_ai_interaction',
    'log_http_request'
]
