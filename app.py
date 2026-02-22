"""Ponto de entrada da aplicação FastAPI."""

from src.infrastructure.logging import setup_structlog
from src.infrastructure.web.app_factory import create_app

# Configurar logging estruturado
setup_structlog()

# Criar app via factory async — uvicorn resolve a coroutine automaticamente
app = create_app()

if __name__ == "__main__":
    import asyncio
    import uvicorn
    from src.infrastructure.logging import app_logger

    uvicorn_config = {
        "app": "app:app",
        "host": "127.0.0.1",
        "port": 7777,
        "reload": True,
        "workers": 1,
        "access_log": False,
        "log_level": "info",
    }

    try:
        import uvloop  # noqa: F401
        uvicorn_config["loop"] = "uvloop"
    except ImportError:
        app_logger.info("uvloop não disponível, usando loop padrão")

    config = uvicorn.Config(**uvicorn_config)
    server = uvicorn.Server(config)
    asyncio.run(server.serve())