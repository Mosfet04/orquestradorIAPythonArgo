"""Configuração da aplicação."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional


@dataclass(frozen=True)
class AppConfig:
    """Configuração imutável da aplicação, carregada a partir de variáveis de ambiente."""

    mongo_connection_string: str
    mongo_database_name: str
    app_title: str
    app_host: str
    app_port: int
    log_level: str
    ollama_base_url: str
    openai_api_key: Optional[str] = None

    # ── OpenTelemetry / Observabilidade ──────────────────────────────
    otel_enabled: bool = True
    otel_exporter_endpoint: str = "http://localhost:4317"
    otel_service_name: str = "orquestrador-ia"

    @classmethod
    def load(cls) -> AppConfig:
        """Carrega e valida configurações a partir de variáveis de ambiente."""
        config = cls(
            mongo_connection_string=os.getenv(
                "MONGO_CONNECTION_STRING",
                "mongodb://localhost:62659/?directConnection=true",
            ),
            mongo_database_name=os.getenv("MONGO_DATABASE_NAME", "agno"),
            app_title=os.getenv("APP_TITLE", "Orquestrador IA Otimizado"),
            app_host=os.getenv("APP_HOST", "127.0.0.1"),
            app_port=int(os.getenv("APP_PORT", "7777")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            otel_enabled=os.getenv("OTEL_ENABLED", "true").lower() in ("true", "1", "yes"),
            otel_exporter_endpoint=os.getenv(
                "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"
            ),
            otel_service_name=os.getenv("OTEL_SERVICE_NAME", "orquestrador-ia"),
        )
        config._validate()
        return config

    def _validate(self) -> None:
        """Valida campos obrigatórios."""
        if not self.mongo_connection_string:
            raise ValueError("MONGO_CONNECTION_STRING é obrigatória")
        if not self.mongo_database_name:
            raise ValueError("MONGO_DATABASE_NAME é obrigatório")
