"""Ponto de entrada da aplicação FastAPI."""

from dotenv import load_dotenv
import asyncio
import sys
load_dotenv()  # carrega .env antes de qualquer acesso a os.getenv()

from src.infrastructure.logging import setup_structlog
from src.infrastructure.web.app_factory import create_app

# Configurar logging estruturado
setup_structlog()

# Criar app via factory síncrona — uvicorn recebe um objeto ASGI real
app = create_app()

if sys.platform == "win32":
    asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())

if __name__ == "__main__":
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