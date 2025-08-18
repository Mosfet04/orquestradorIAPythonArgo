"""
Ponto de entrada da aplicação FastAPI.

Este arquivo deve conter apenas a configuração mínima de inicialização,
delegando toda a lógica de criação da aplicação para a camada de Infrastructure.
"""
import asyncio
from src.infrastructure.web.app_factory import create_app
from src.infrastructure.logging import setup_structlog, app_logger

# Configurar logging estruturado na inicialização
setup_structlog()

# Criar instância da aplicação
app = create_app()

if __name__ == "__main__":
    import uvicorn
    
    # Configurações otimizadas do uvicorn
    uvicorn_config = {
        "app": "app:app",
        "host": "127.0.0.1",
        "port": 7777,
        "reload": True,
        "workers": 1,
        "access_log": False,
        "log_level": "info",
        "backlog": 2048,
        "h11_max_incomplete_event_size": 16384
    }
    
    try:
        import uvloop
        uvicorn_config["loop"] = "uvloop"
    except ImportError:
        app_logger.info("⚠️ uvloop não disponível, usando loop padrão")
    
    config = uvicorn.Config(**uvicorn_config)
    server = uvicorn.Server(config)
    asyncio.run(server.serve())