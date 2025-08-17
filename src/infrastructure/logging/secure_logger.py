"""
Sistema de logging seguro para o Orquestrador IA.
Implementa sanitização de dados sensíveis e estruturação para análise eficiente.
"""

import logging
import json
import re
import traceback
from typing import Any, Dict, Optional, List, Sequence
from datetime import datetime
from enum import Enum
from dataclasses import dataclass, asdict
import hashlib


class LogLevel(Enum):
    """Níveis de log personalizados."""
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"
    SECURITY = "SECURITY"  # Eventos de segurança
    PERFORMANCE = "PERFORMANCE"  # Métricas de performance
    AI_REQUEST = "AI_REQUEST"  # Requisições específicas de IA


@dataclass
class LogContext:
    """Contexto estruturado para logs."""
    request_id: Optional[str] = None
    user_id: Optional[str] = None
    agent_id: Optional[str] = None
    tool_id: Optional[str] = None
    session_id: Optional[str] = None
    correlation_id: Optional[str] = None


class DataSanitizer:
    """Sanitizador de dados sensíveis para logs."""
    
    # Padrões de dados sensíveis
    SENSITIVE_PATTERNS = {
        'api_key': re.compile(r'(?i)(api[_-]?key|apikey)[\s]*[=:][\s]*["\']?([a-zA-Z0-9_-]{20,})["\']?'),
        'token': re.compile(r'(?i)(token|bearer)[\s]*[=:][\s]*["\']?([a-zA-Z0-9_.-]{20,})["\']?'),
        'password': re.compile(r'(?i)(password|passwd|pwd)[\s]*[=:][\s]*["\']?([^"\'\s]{6,})["\']?'),
        'mongodb_uri': re.compile(r'mongodb(\+srv)?://[^:]+:[^@]+@[^/]+'),
        'email': re.compile(r'\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Z|a-z]{2,}\b'),
        'cartao_credito': re.compile(r'\b\d{4}[\s-]?\d{4}[\s-]?\d{4}[\s-]?\d{4}\b'),
        'cpf': re.compile(r'\b\d{3}\.?\d{3}\.?\d{3}-?\d{2}\b'),
        'cnpj': re.compile(r'\b\d{2}\.?\d{3}\.?\d{3}/?\d{4}-?\d{2}\b'),
    }
    
    # Campos que devem ser sempre sanitizados
    SENSITIVE_FIELDS = {
        'password', 'passwd', 'pwd', 'senha', 'api_key', 'apikey', 'token', 'secret',
        'authorization', 'auth', 'credential', 'private_key', 'access_token',
        'refresh_token', 'session_id', 'connection_string', 'database_url'
    }
    
    @classmethod
    def sanitize_data(cls, data: Any, max_depth: int = 5) -> Any:
        """
        Sanitiza dados removendo informações sensíveis.
        
        Args:
            data: Dados a serem sanitizados
            max_depth: Profundidade máxima para recursão
            
        Returns:
            Dados sanitizados
        """
        if max_depth <= 0:
            return "[MAX_DEPTH_REACHED]"
            
        if isinstance(data, str):
            return cls._sanitize_string(data)
        elif isinstance(data, dict):
            return cls._sanitize_dict(data, max_depth - 1)
        elif isinstance(data, (list, tuple)):
            return cls._sanitize_list(data, max_depth - 1)
        else:
            return data
    
    # Mapa auxiliar para descobrir o nome do padrão a partir do regex usado no match
    _PATTERN_TO_NAME = {pattern: name for name, pattern in SENSITIVE_PATTERNS.items()}

    @staticmethod
    def _mask_match(m: re.Match) -> str:
        """Callback único para mascarar ocorrências encontradas pelos padrões."""
        pattern = m.re
        pattern_name = DataSanitizer._PATTERN_TO_NAME.get(pattern, "SENSITIVE").upper()
        # Se houver grupo capturado, usar o primeiro grupo como chave; senão, só mascarar
        try:
            grp1 = m.group(1) if m.re.groups >= 1 else None
        except IndexError:
            grp1 = None
        if grp1:
            return f"{grp1}=***{pattern_name}_MASKED***"
        return f"***{pattern_name}_MASKED***"

    @classmethod
    def _sanitize_string(cls, text: str) -> str:
        """Sanitiza uma string removendo dados sensíveis."""
        if not isinstance(text, str) or len(text) == 0:
            return text

        # Limitar tamanho do log para performance
        if len(text) > 10000:
            text = text[:10000] + "...[TRUNCATED]"

        sanitized = text

        # Aplicar padrões de sanitização (um único pass de sub por padrão, sem função criada por iteração)
        for _name, pattern in cls.SENSITIVE_PATTERNS.items():
            sanitized = pattern.sub(cls._mask_match, sanitized)

        return sanitized
    
    @classmethod
    def _sanitize_dict(cls, data: Dict[str, Any], max_depth: int) -> Dict[str, Any]:
        """Sanitiza um dicionário."""
        sanitized = {}
        
        for key, value in data.items():
            # Verificar se a chave é sensível
            if any(sensitive in key.lower() for sensitive in cls.SENSITIVE_FIELDS):
                if isinstance(value, str) and len(value) > 0:
                    # Mostrar apenas primeiros/últimos caracteres para debug
                    masked_value = cls._create_masked_value(value)
                    sanitized[key] = masked_value
                else:
                    sanitized[key] = "***MASKED***"
            else:
                sanitized[key] = cls.sanitize_data(value, max_depth)
        
        return sanitized
    
    @classmethod
    def _sanitize_list(cls, data: Sequence[Any], max_depth: int) -> List[Any]:
        """Sanitiza uma lista (ou tupla), retornando uma nova lista."""
        # Limitar tamanho da lista para performance
        if len(data) > 100:
            sliced = list(data[:100])
            sliced.append("[LIST_TRUNCATED]")
        else:
            sliced = list(data)

        return [cls.sanitize_data(item, max_depth) for item in sliced]
    
    @classmethod
    def _create_masked_value(cls, value: str) -> str:
        """Cria uma versão mascarada do valor mantendo informações para debug."""
        if len(value) <= 8:
            return "***MASKED***"
        
        # Mostrar primeiros 2 e últimos 2 caracteres
        return f"{value[:2]}***{value[-2:]}[len:{len(value)}]"
    
    @classmethod
    def hash_sensitive_data(cls, data: str) -> str:
        """Cria hash de dados sensíveis para correlação sem exposição."""
        return hashlib.sha256(data.encode()).hexdigest()[:16]


class SecureLogger:
    """Logger seguro com sanitização automática e estruturação."""
    
    def __init__(self, name: str, level: str = "INFO"):
        self.logger = logging.getLogger(name)
        self.logger.setLevel(getattr(logging, level))
        
        # Evitar duplicação de handlers
        if not self.logger.handlers:
            self._setup_handlers()
        
        self.context = LogContext()
    
    def _setup_handlers(self):
        """Configura os handlers de log."""
        # Handler para console (desenvolvimento)
        console_handler = logging.StreamHandler()
        console_handler.setLevel(logging.INFO)
        
        # Handler para arquivo (produção)
        file_handler = logging.FileHandler('logs/app.log', encoding='utf-8')
        file_handler.setLevel(logging.DEBUG)
        
        # Formatter estruturado JSON
        formatter = logging.Formatter(
            '%(asctime)s | %(name)s | %(levelname)s | %(message)s',
            datefmt='%Y-%m-%d %H:%M:%S'
        )
        
        console_handler.setFormatter(formatter)
        file_handler.setFormatter(formatter)
        
        self.logger.addHandler(console_handler)
        self.logger.addHandler(file_handler)
    
    def set_context(self, **kwargs):
        """Define o contexto para os próximos logs."""
        for key, value in kwargs.items():
            if hasattr(self.context, key):
                setattr(self.context, key, value)
    
    def clear_context(self):
        """Limpa o contexto atual."""
        self.context = LogContext()
    
    def _create_log_entry(self, level: LogLevel, message: str, data: Optional[Dict] = None, 
                         exception: Optional[Exception] = None) -> Dict[str, Any]:
        """Cria entrada de log estruturada."""
        log_entry = {
            'timestamp': datetime.utcnow().isoformat(),
            'level': level.value,
            'message': message,
            'context': asdict(self.context),
            'logger': self.logger.name
        }
        
        # Adicionar dados sanitizados
        if data:
            log_entry['data'] = DataSanitizer.sanitize_data(data)
        
        # Adicionar informações de exceção
        if exception:
            log_entry['exception'] = {
                'type': exception.__class__.__name__,
                'message': str(exception),
                'traceback': traceback.format_exc()
            }
        
        return log_entry
    
    def _log(self, level: LogLevel, message: str, data: Optional[Dict] = None, 
             exception: Optional[Exception] = None):
        """Método interno para logging."""
        log_entry = self._create_log_entry(level, message, data, exception)
        log_message = json.dumps(log_entry, ensure_ascii=False, separators=(',', ':'))
        
        # Mapear para níveis padrão do logging
        level_mapping = {
            LogLevel.DEBUG: logging.DEBUG,
            LogLevel.INFO: logging.INFO,
            LogLevel.WARNING: logging.WARNING,
            LogLevel.ERROR: logging.ERROR,
            LogLevel.CRITICAL: logging.CRITICAL,
            LogLevel.SECURITY: logging.WARNING,
            LogLevel.PERFORMANCE: logging.INFO,
            LogLevel.AI_REQUEST: logging.INFO
        }
        
        self.logger.log(level_mapping[level], log_message)
    
    # Métodos públicos de logging
    def debug(self, message: str, data: Optional[Dict] = None):
        """Log de debug."""
        self._log(LogLevel.DEBUG, message, data)
    
    def info(self, message: str, data: Optional[Dict] = None):
        """Log de informação."""
        self._log(LogLevel.INFO, message, data)
    
    def warning(self, message: str, data: Optional[Dict] = None):
        """Log de aviso."""
        self._log(LogLevel.WARNING, message, data)
    
    def error(self, message: str, data: Optional[Dict] = None, exception: Optional[Exception] = None):
        """Log de erro."""
        self._log(LogLevel.ERROR, message, data, exception)
    
    def critical(self, message: str, data: Optional[Dict] = None, exception: Optional[Exception] = None):
        """Log crítico."""
        self._log(LogLevel.CRITICAL, message, data, exception)
    
    def security(self, message: str, data: Optional[Dict] = None):
        """Log de evento de segurança."""
        self._log(LogLevel.SECURITY, message, data)
    
    def performance(self, message: str, data: Optional[Dict] = None):
        """Log de performance."""
        self._log(LogLevel.PERFORMANCE, message, data)
    
    def ai_request(self, message: str, data: Optional[Dict] = None):
        """Log específico para requisições de IA."""
        self._log(LogLevel.AI_REQUEST, message, data)


# Factory para criar loggers
class LoggerFactory:
    """Factory para criar instâncias de logger."""
    
    _loggers: Dict[str, SecureLogger] = {}
    
    @classmethod
    def get_logger(cls, name: str, level: str = "INFO") -> SecureLogger:
        """Obtém ou cria um logger."""
        if name not in cls._loggers:
            cls._loggers[name] = SecureLogger(name, level)
        return cls._loggers[name]


# Logger principal da aplicação
app_logger = LoggerFactory.get_logger("orquestrador_ia")
