"""
Sistema de logging seguro usando structlog para o Orquestrador IA.
Otimizado para ambientes cloud (AWS CloudWatch, ELK Stack, etc.).
"""

import structlog
import logging
import re
import traceback
import os
import sys
from typing import Any, Dict, Optional, List
from datetime import datetime, timezone
from enum import Enum
from dataclasses import dataclass, asdict
import hashlib
from pathlib import Path


class LogLevel(Enum):
    """Níveis de log personalizados."""
    DEBUG = "debug"
    INFO = "info"
    WARNING = "warning"
    ERROR = "error"
    CRITICAL = "critical"
    SECURITY = "security"  # Eventos de segurança
    PERFORMANCE = "performance"  # Métricas de performance
    AI_REQUEST = "ai_request"  # Requisições específicas de IA


@dataclass
class LogContext:
    """Contexto estruturado para logs."""
    request_id: Optional[str] = None
    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    tool_id: Optional[str] = None
    session_id: Optional[str] = None
    correlation_id: Optional[str] = None
    service_name: str = "orquestrador-ia"
    environment: str = "development"


class DataSanitizer:
    """Sanitizador de dados sensíveis para logs cloud-ready."""
    
    # Padrões de dados sensíveis
    SENSITIVE_PATTERNS = {
        'api_key': re.compile(r'(?i)(api[_-]?key|apikey)[\s]*[=:][\s]*["\']?([a-zA-Z0-9_-]{20,})["\']?'),
        'token': re.compile(r'(?i)(token|bearer)[\s]*[=:][\s]*["\']?([a-zA-Z0-9_.-]{20,})["\']?'),
        'password': re.compile(r'(?i)(password|passwd|pwd)[\s]*[=:][\s]*["\']?([^"\'\s]{6,})["\']?'),
        'mongodb_uri': re.compile(r'mongodb(\+srv)?://[^:]+:[^@]+@[^/]+'),
        'email': re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'),
        'credit_card': re.compile(r'\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b'),
        'cpf': re.compile(r'\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b'),
        'cnpj': re.compile(r'\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b'),
        'aws_access_key': re.compile(r'AKIA[0-9A-Z]{16}'),
        'aws_secret_key': re.compile(r'[0-9a-zA-Z/+]{40}'),
    }
    
    # Campos que devem ser sempre sanitizados
    SENSITIVE_FIELDS = {
        'password', 'passwd', 'pwd', 'api_key', 'apikey', 'token', 'secret',
        'authorization', 'auth', 'credential', 'private_key', 'access_token',
        'refresh_token', 'session_id', 'connection_string', 'database_url',
        'aws_access_key_id', 'aws_secret_access_key', 'database_password',
        'mongo_password', 'redis_password', 'jwt_secret', 'encryption_key'
    }
    
    @classmethod
    def sanitize_data(cls, data: Any, max_depth: int = 5) -> Any:
        """
        Sanitiza dados removendo informações sensíveis.
        Otimizado para logs estruturados em cloud.
        
        Args:
            data: Dados a serem sanitizados
            max_depth: Profundidade máxima para recursão
            
        Returns:
            Dados sanitizados
        """
        if max_depth <= 0:
            return {"_truncated": "max_depth_reached", "_type": type(data).__name__}
            
        if isinstance(data, str):
            return cls._sanitize_string(data)
        elif isinstance(data, dict):
            return cls._sanitize_dict(data, max_depth - 1)
        elif isinstance(data, (list, tuple)):
            return cls._sanitize_list(list(data), max_depth - 1)
        elif isinstance(data, (int, float, bool)):
            return data
        elif data is None:
            return None
        else:
            # Para outros tipos, converter para string e sanitizar
            return cls._sanitize_string(str(data))
    
    @classmethod
    def _sanitize_string(cls, text: str) -> str:
        """Sanitiza uma string removendo dados sensíveis."""
        if not isinstance(text, str) or len(text) == 0:
            return text
            
        # Limitar tamanho do log para performance em cloud
        if len(text) > 5000:  # Reduzido para cloud
            text = text[:5000] + "...[TRUNCATED]"
        
        sanitized = text
        
        # Aplicar padrões de sanitização
        for pattern_name, pattern in cls.SENSITIVE_PATTERNS.items():
            if pattern.search(sanitized):
                sanitized = pattern.sub(
                    lambda m: f"{m.group(1) if m.groups() else ''}=***{pattern_name.upper()}_MASKED***", 
                    sanitized
                )
        
        return sanitized
    
    @classmethod
    def _sanitize_dict(cls, data: Dict[str, Any], max_depth: int) -> Dict[str, Any]:
        """Sanitiza um dicionário."""
        sanitized = {}
        
        for key, value in data.items():
            # Verificar se a chave é sensível (busca mais rigorosa)
            key_lower = key.lower()
            is_sensitive = any(sensitive in key_lower for sensitive in cls.SENSITIVE_FIELDS)
            
            if is_sensitive:
                if isinstance(value, str) and len(value) > 0:
                    # Criar hash para correlação sem exposição
                    value_hash = cls.hash_sensitive_data(value)
                    sanitized[key] = f"***MASKED***[hash:{value_hash}]"
                else:
                    sanitized[key] = "***MASKED***"
            else:
                sanitized[key] = cls.sanitize_data(value, max_depth)
        
        return sanitized
    
    @classmethod
    def _sanitize_list(cls, data: List[Any], max_depth: int) -> List[Any]:
        """Sanitiza uma lista."""
        # Limitar tamanho da lista para performance em cloud
        if len(data) > 50:  # Reduzido para cloud
            sanitized_data = data[:50]
            return [cls.sanitize_data(item, max_depth) for item in sanitized_data] + [
                {"_truncated": f"list_truncated_from_{len(data)}_items"}
            ]
        else:
            return [cls.sanitize_data(item, max_depth) for item in data]
    
    @classmethod
    def hash_sensitive_data(cls, data: str) -> str:
        """Cria hash de dados sensíveis para correlação sem exposição."""
        return hashlib.sha256(data.encode()).hexdigest()[:12]


def add_correlation_id(logger, method_name, event_dict):
    """Adiciona correlation_id único para rastreamento."""
    if 'correlation_id' not in event_dict:
        import uuid
        event_dict['correlation_id'] = str(uuid.uuid4())[:8]
    return event_dict


def add_timestamp(logger, method_name, event_dict):
    """Adiciona timestamp ISO para compatibilidade cloud."""
    event_dict['timestamp'] = datetime.now(timezone.utc).isoformat()
    return event_dict


def add_service_metadata(logger, method_name, event_dict):
    """Adiciona metadados do serviço para cloud."""
    event_dict.update({
        'service': 'orquestrador-ia',
        'version': '1.0.0',
        'environment': os.getenv('ENVIRONMENT', 'development'),
        'hostname': os.getenv('HOSTNAME', 'localhost'),
        'aws_region': os.getenv('AWS_REGION', 'us-east-1'),
        'log_level': method_name,
    })
    return event_dict


def sanitize_log_data(logger, method_name, event_dict):
    """Sanitiza dados sensíveis nos logs."""
    # Campos que não devem ser sanitizados (metadados do sistema)
    protected_fields = {
        'timestamp', 'level', 'logger', 'service', 'version', 'environment',
        'aws_region', 'hostname', 'correlation_id', 'log_level', 'service_name',
        'event', 'user_id', 'request_id', 'test_type', 'operation', 'status',
        'records_processed', 'duration_seconds', 'error_type', 'error_message',
        'action', 'kubernetes_pod', 'trace_id'
    }
    
    # Campos sensíveis definidos
    sensitive_fields = {
        'password', 'passwd', 'pwd', 'api_key', 'apikey', 'token', 'secret',
        'authorization', 'auth', 'credential', 'private_key', 'access_token',
        'refresh_token', 'session_id', 'connection_string', 'database_url',
        'aws_access_key_id', 'aws_secret_access_key', 'database_password',
        'mongo_password', 'redis_password', 'jwt_secret', 'encryption_key'
    }
    
    # Sanitizar campos específicos
    for key, value in list(event_dict.items()):
        if key not in protected_fields:
            # Verificar se o campo é sensível
            key_lower = key.lower()
            is_sensitive = any(sensitive in key_lower for sensitive in sensitive_fields)
            
            if is_sensitive:
                if isinstance(value, str) and len(value) > 0:
                    # Criar hash para correlação sem exposição
                    value_hash = DataSanitizer.hash_sensitive_data(value)
                    event_dict[key] = f"***MASKED***[hash:{value_hash}]"
                else:
                    event_dict[key] = "***MASKED***"
            else:
                # Para campos não sensíveis, aplicar sanitização geral
                event_dict[key] = DataSanitizer.sanitize_data(value)
    
    return event_dict


def setup_structlog():
    """Configura structlog para ambiente cloud-ready."""
    
    # Criar diretório de logs se não existir
    log_dir = Path("logs")
    log_dir.mkdir(exist_ok=True)
    
    # Configurar logging padrão
    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=logging.INFO,
    )
    
    # Procesadores para desenvolvimento
    dev_processors = [
        structlog.contextvars.merge_contextvars,
        add_correlation_id,
        add_timestamp,
        add_service_metadata,
        sanitize_log_data,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.dev.ConsoleRenderer(colors=True)
    ]
    
    # Procesadores para produção (cloud)
    prod_processors = [
        structlog.contextvars.merge_contextvars,
        add_correlation_id,
        add_timestamp,
        add_service_metadata,
        sanitize_log_data,
        structlog.processors.add_log_level,
        structlog.processors.StackInfoRenderer(),
        structlog.processors.dict_tracebacks,
        structlog.processors.JSONRenderer()
    ]
    
    # Escolher processadores baseado no ambiente
    environment = os.getenv('ENVIRONMENT', 'development').lower()
    processors = prod_processors if environment == 'production' else dev_processors
    
    # Configurar structlog
    structlog.configure(
        processors=processors,
        context_class=dict,
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.stdlib.BoundLogger,
        cache_logger_on_first_use=True,
    )


class StructlogLogger:
    """Logger wrapper usando structlog com funcionalidades específicas para cloud."""
    
    def __init__(self, name: str):
        self.name = name
        self.logger = structlog.get_logger(name)
        self.context = LogContext()
    
    def bind(self, **kwargs) -> 'StructlogLogger':
        """Vincula contexto ao logger."""
        new_logger = StructlogLogger(self.name)
        new_logger.logger = self.logger.bind(**kwargs)
        new_logger.context = self.context
        return new_logger
    
    def set_context(self, **kwargs):
        """Define o contexto para os próximos logs."""
        for key, value in kwargs.items():
            if hasattr(self.context, key):
                setattr(self.context, key, value)
        
        # Aplicar contexto ao logger
        context_dict = {k: v for k, v in asdict(self.context).items() if v is not None}
        self.logger = self.logger.bind(**context_dict)
    
    def clear_context(self):
        """Limpa o contexto atual."""
        self.context = LogContext()
        self.logger = structlog.get_logger(self.name)
    
    # Métodos de logging padrão
    def debug(self, message: str, **kwargs):
        """Log de debug."""
        self.logger.debug(message, **kwargs)
    
    def info(self, message: str, **kwargs):
        """Log de informação."""
        self.logger.info(message, **kwargs)
    
    def warning(self, message: str, **kwargs):
        """Log de aviso."""
        self.logger.warning(message, **kwargs)
    
    def error(self, message: str, exception: Optional[Exception] = None, **kwargs):
        """Log de erro."""
        if exception:
            kwargs['exception_type'] = exception.__class__.__name__
            kwargs['exception_message'] = str(exception)
            kwargs['exception_traceback'] = traceback.format_exc()
        self.logger.error(message, **kwargs)
    
    def critical(self, message: str, exception: Optional[Exception] = None, **kwargs):
        """Log crítico."""
        if exception:
            kwargs['exception_type'] = exception.__class__.__name__
            kwargs['exception_message'] = str(exception)
            kwargs['exception_traceback'] = traceback.format_exc()
        self.logger.critical(message, **kwargs)
    
    # Métodos específicos para cloud
    def security(self, message: str, **kwargs):
        """Log de evento de segurança."""
        kwargs['log_type'] = 'security_event'
        kwargs['severity'] = 'high'
        self.logger.warning(message, **kwargs)
    
    def performance(self, message: str, **kwargs):
        """Log de performance."""
        kwargs['log_type'] = 'performance_metric'
        self.logger.info(message, **kwargs)
    
    def ai_request(self, message: str, **kwargs):
        """Log específico para requisições de IA."""
        kwargs['log_type'] = 'ai_interaction'
        self.logger.info(message, **kwargs)
    
    def business_event(self, message: str, **kwargs):
        """Log para eventos de negócio importantes."""
        kwargs['log_type'] = 'business_event'
        self.logger.info(message, **kwargs)
    
    def audit(self, message: str, **kwargs):
        """Log de auditoria."""
        kwargs['log_type'] = 'audit_log'
        kwargs['severity'] = 'high'
        self.logger.info(message, **kwargs)


class LoggerFactory:
    """Factory para criar instâncias de logger structlog."""
    
    _loggers: Dict[str, StructlogLogger] = {}
    _initialized = False
    
    @classmethod
    def _ensure_initialized(cls):
        """Garante que o structlog foi inicializado."""
        if not cls._initialized:
            setup_structlog()
            cls._initialized = True
    
    @classmethod
    def get_logger(cls, name: str) -> StructlogLogger:
        """Obtém ou cria um logger."""
        cls._ensure_initialized()
        
        if name not in cls._loggers:
            cls._loggers[name] = StructlogLogger(name)
        return cls._loggers[name]


# Logger principal da aplicação
app_logger = LoggerFactory.get_logger("orquestrador_ia")
