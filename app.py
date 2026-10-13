"""Ponto de entrada da aplicação FastAPI."""

import asyncio
import sys

from dotenv import load_dotenv

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

    from src.infrastructure.config.app_config import AppConfig
    from src.infrastructure.logging import app_logger
    from src.infrastructure.web.server_settings import build_uvicorn_settings

    # APP_HOST/APP_PORT do ambiente (default 127.0.0.1:7777; o Dockerfile usa 0.0.0.0)
    uvicorn_config = build_uvicorn_settings(AppConfig.load())

    # uvloop não existe no Windows (nem é instalado lá): import guardado, só fora dele.
    if sys.platform != "win32":
        try:
            import uvloop

            asyncio.set_event_loop_policy(uvloop.EventLoopPolicy())
            uvicorn_config["loop"] = "uvloop"
        except ImportError:
            app_logger.info("uvloop não disponível, usando loop padrão")

    config = uvicorn.Config(**uvicorn_config)
    server = uvicorn.Server(config)
    asyncio.run(server.serve())
