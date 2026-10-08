"""Configuração da aplicação."""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional
from urllib.parse import urlsplit

ENVIRONMENTS = ("development", "test", "staging", "production")

# Origens do CORS quando CORS_ALLOWED_ORIGINS não está definida (as de antes do F1-03).
DEFAULT_CORS_ALLOWED_ORIGINS = (
    "https://app.agno.com",
    "https://www.agno.com",
    "http://localhost:3000",
    "http://localhost:7777",
    "https://os.agno.com",
)

_TRUE = ("true", "1", "yes", "on")
_FALSE = ("false", "0", "no", "off")


@dataclass(frozen=True)
class AppConfig:
    """Configuração imutável da aplicação, carregada a partir de variáveis de ambiente."""

    mongo_connection_string: str
    mongo_database_name: str
    app_title: str
    app_host: str
    app_port: int
    log_level: str
    # None = defaults do cliente ollama/agno (OLLAMA_HOST, localhost ou Ollama Cloud com chave)
    ollama_base_url: Optional[str]
    openai_api_key: Optional[str] = None

    # ── OpenTelemetry / Observabilidade ──────────────────────────────
    otel_enabled: bool = True
    otel_exporter_endpoint: str = "http://localhost:4317"
    otel_service_name: str = "orquestrador-ia"

    # ── Borda HTTP ───────────────────────────────────────────────────
    environment: str = "development"
    # /docs, /redoc e /openapi.json; load() liga por padrão só em development
    enable_docs: bool = False
    cors_allowed_origins: tuple[str, ...] = DEFAULT_CORS_ALLOWED_ORIGINS

    @classmethod
    def load(cls) -> AppConfig:
        """Carrega e valida configurações a partir de variáveis de ambiente."""
        environment = _environment()
        config = cls(
            mongo_connection_string=os.getenv(
                "MONGO_CONNECTION_STRING",
                "mongodb://localhost:62659/?directConnection=true",
            ),
            mongo_database_name=os.getenv("MONGO_DATABASE_NAME", "agno"),
            app_title=os.getenv("APP_TITLE", "Orquestrador IA Otimizado"),
            # Vazio/em branco cai no loopback: host "" faria bind em todas as interfaces.
            app_host=(os.getenv("APP_HOST") or "").strip() or "127.0.0.1",
            app_port=int(os.getenv("APP_PORT", "7777")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            ollama_base_url=(os.getenv("OLLAMA_BASE_URL") or "").strip() or None,
            openai_api_key=os.getenv("OPENAI_API_KEY"),
            otel_enabled=os.getenv("OTEL_ENABLED", "true").lower() in ("true", "1", "yes"),
            otel_exporter_endpoint=os.getenv(
                "OTEL_EXPORTER_OTLP_ENDPOINT", "http://localhost:4317"
            ),
            otel_service_name=os.getenv("OTEL_SERVICE_NAME", "orquestrador-ia"),
            environment=environment,
            enable_docs=_optional_bool(
                "ENABLE_DOCS", os.getenv("ENABLE_DOCS"), default=environment == "development"
            ),
            cors_allowed_origins=_cors_allowed_origins(),
        )
        config._validate()
        return config

    def _validate(self) -> None:
        """Valida campos obrigatórios."""
        if not self.mongo_connection_string:
            raise ValueError("MONGO_CONNECTION_STRING é obrigatória")
        if not self.mongo_database_name:
            raise ValueError("MONGO_DATABASE_NAME é obrigatório")


def _environment() -> str:
    """``ENVIRONMENT`` normalizado; vazio = development, fora da lista = erro."""
    value = (os.getenv("ENVIRONMENT") or "").strip().lower() or "development"
    if value not in ENVIRONMENTS:
        raise ValueError(
            f"ENVIRONMENT inválido: {value!r}. Use um de: {', '.join(ENVIRONMENTS)}"
        )
    return value


def _optional_bool(name: str, raw: Optional[str], *, default: bool) -> bool:
    """Booleano de env; vazio = ``default``, valor desconhecido = erro (não adivinha)."""
    value = (raw or "").strip().lower()
    if not value:
        return default
    if value in _TRUE:
        return True
    if value in _FALSE:
        return False
    raise ValueError(f"{name} inválido: {value!r}. Use true ou false")


def _cors_allowed_origins() -> tuple[str, ...]:
    """Origens separadas por vírgula; vazio = ``DEFAULT_CORS_ALLOWED_ORIGINS``.

    Cada origem é ``http(s)://host[:porta]``, exatamente como o navegador manda no header
    ``Origin`` (o Starlette compara a string inteira). ``*`` é recusado: com
    ``allow_credentials`` o Starlette ecoaria qualquer origem.
    """
    raw = os.getenv("CORS_ALLOWED_ORIGINS") or ""
    origins = tuple(origin.strip() for origin in raw.split(",") if origin.strip())
    for origin in origins:
        _validate_origin(origin)
    return origins or DEFAULT_CORS_ALLOWED_ORIGINS


def _validate_origin(origin: str) -> None:
    def invalid(reason: str) -> ValueError:
        return ValueError(
            f"CORS_ALLOWED_ORIGINS inválida: {origin!r} ({reason}); "
            "use http(s)://host[:porta], sem path, query, fragment nem '*'"
        )

    if origin == "*":
        raise invalid("'*' não é aceito com credenciais")
    if origin.lower() == "null":
        raise invalid("origem null não é aceita")
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https"):
        raise invalid("esquema deve ser http ou https")
    if not parts.hostname:
        raise invalid("sem host")
    if parts.username is not None or parts.password is not None:
        raise invalid("credenciais na URL não são aceitas")
    try:
        parts.port  # noqa: B018 - valida a porta (levanta ValueError se não for número)
    except ValueError:
        raise invalid("porta inválida") from None
    if parts.path == "/":
        raise invalid("barra final não faz parte da origem")
    if parts.path:
        raise invalid("path não faz parte da origem")
    if parts.query or "?" in origin:
        raise invalid("query não faz parte da origem")
    if parts.fragment or "#" in origin:
        raise invalid("fragment não faz parte da origem")
