"""
Configuração de logging para a aplicação Orquestrador IA.
"""

import os
import logging.config
from pathlib import Path

# Criar diretório de logs se não existir
log_dir = Path("logs")
log_dir.mkdir(exist_ok=True)

# Configuração do sistema de logging
LOGGING_CONFIG = {
    'version': 1,
    'disable_existing_loggers': False,
    'formatters': {
        'detailed': {
            'format': '%(asctime)s | %(name)s | %(levelname)s | %(message)s',
            'datefmt': '%Y-%m-%d %H:%M:%S'
        },
        'simple': {
            'format': '%(levelname)s | %(message)s'
        }
    },
    'handlers': {
        'console': {
            'class': 'logging.StreamHandler',
            'level': 'INFO',
            'formatter': 'simple',
            'stream': 'ext://sys.stdout'
        },
        'file_app': {
            'class': 'logging.handlers.RotatingFileHandler',
            'level': 'DEBUG',
            'formatter': 'detailed',
            'filename': 'logs/app.log',
            'maxBytes': 100 * 1024 * 1024,  # 100MB
            'backupCount': 10,
            'encoding': 'utf-8'
        },
        'file_security': {
            'class': 'logging.handlers.RotatingFileHandler',
            'level': 'WARNING',
            'formatter': 'detailed',
            'filename': 'logs/security.log',
            'maxBytes': 50 * 1024 * 1024,  # 50MB
            'backupCount': 20,
            'encoding': 'utf-8'
        },
        'file_performance': {
            'class': 'logging.handlers.RotatingFileHandler',
            'level': 'INFO',
            'formatter': 'detailed',
            'filename': 'logs/performance.log',
            'maxBytes': 50 * 1024 * 1024,  # 50MB
            'backupCount': 5,
            'encoding': 'utf-8'
        },
        'file_ai': {
            'class': 'logging.handlers.RotatingFileHandler',
            'level': 'INFO',
            'formatter': 'detailed',
            'filename': 'logs/ai_interactions.log',
            'maxBytes': 100 * 1024 * 1024,  # 100MB
            'backupCount': 10,
            'encoding': 'utf-8'
        },
        'file_http': {
            'class': 'logging.handlers.RotatingFileHandler',
            'level': 'INFO',
            'formatter': 'detailed',
            'filename': 'logs/http_requests.log',
            'maxBytes': 50 * 1024 * 1024,  # 50MB
            'backupCount': 10,
            'encoding': 'utf-8'
        }
    },
    'loggers': {
        'orquestrador_ia': {
            'level': 'DEBUG',
            'handlers': ['console', 'file_app'],
            'propagate': False
        },
        'http_tool_factory': {
            'level': 'DEBUG',
            'handlers': ['console', 'file_app'],
            'propagate': False
        },
        'http_tool_executions': {
            'level': 'INFO',
            'handlers': ['file_http'],
            'propagate': False
        },
        'mongo_tool_repository': {
            'level': 'DEBUG',
            'handlers': ['console', 'file_app'],
            'propagate': False
        },
        'model_factory': {
            'level': 'DEBUG',
            'handlers': ['console', 'file_ai'],
            'propagate': False
        },
        'ai_interactions': {
            'level': 'INFO',
            'handlers': ['file_ai'],
            'propagate': False
        },
        'performance': {
            'level': 'INFO',
            'handlers': ['file_performance'],
            'propagate': False
        },
        'security': {
            'level': 'WARNING',
            'handlers': ['console', 'file_security'],
            'propagate': False
        }
    },
    'root': {
        'level': 'INFO',
        'handlers': ['console', 'file_app']
    }
}


def setup_logging():
    """Configura o sistema de logging da aplicação."""
    # Garantir que o diretório de logs exista no diretório de trabalho atual
    Path("logs").mkdir(exist_ok=True, parents=True)

    # Ajustar nível baseado em variável de ambiente
    log_level = os.getenv('LOG_LEVEL', 'INFO').upper()
    
    # Aplicar nível aos handlers de console
    LOGGING_CONFIG['handlers']['console']['level'] = log_level
    
    # Configurar logging
    logging.config.dictConfig(LOGGING_CONFIG)
    
    # Log inicial
    logger = logging.getLogger('orquestrador_ia')


# Configurar logging automaticamente na importação do módulo
setup_logging()
