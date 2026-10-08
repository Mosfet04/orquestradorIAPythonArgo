"""Parâmetros do servidor uvicorn derivados do ``AppConfig``."""

from __future__ import annotations

from src.infrastructure.config.app_config import AppConfig


def build_uvicorn_settings(config: AppConfig) -> dict[str, object]:
    """Monta os kwargs de ``uvicorn.Config`` com host/porta vindos do ``AppConfig``.

    Fora do container o default de ``APP_HOST`` é ``127.0.0.1``; a imagem Docker
    define ``APP_HOST=0.0.0.0``.
    """
    return {
        "app": "app:app",
        "host": config.app_host,
        "port": config.app_port,
        # Sem efeito com uvicorn.Server.serve() (o reloader vive em uvicorn.run).
        "reload": False,
        "workers": 1,
        "access_log": False,
        "log_level": "info",
    }
