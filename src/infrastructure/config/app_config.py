"""Configuração da aplicação."""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from typing import Optional
from urllib.parse import urlsplit

from src.infrastructure.config.secrets import DEFAULT_SECRETS_DIR
from src.infrastructure.security.ssrf import normalize_host

ENVIRONMENTS = ("development", "test", "staging", "production")

# Origens do CORS quando CORS_ALLOWED_ORIGINS não está definida (as de antes do F1-03).
DEFAULT_CORS_ALLOWED_ORIGINS = (
    "https://app.agno.com",
    "https://www.agno.com",
    "http://localhost:3000",
    "http://localhost:7777",
    "https://os.agno.com",
)

# Chaves de API da borda (F1-04): tamanho mínimo e como gerar uma.
API_KEY_MIN_LENGTH = 32
API_KEY_HINT = 'python -c "import secrets; print(secrets.token_urlsafe(32))"'

# Rótulo DNS (RFC 1123): letras, dígitos e hífen, sem hífen nas pontas, até 63 caracteres.
_DNS_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?")

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
    # Chaves de API (F1-04). As duas ou nenhuma; fora do repr para não irem parar em log.
    api_key_run: Optional[str] = field(default=None, repr=False)
    api_key_admin: Optional[str] = field(default=None, repr=False)
    # Raiz dos api_key_ref "file:" (F2-01); repassada ao resolve_secret pelo composition root.
    secrets_dir: str = DEFAULT_SECRETS_DIR
    # Hosts aceitos em base_url de modelo/embedder vinda da config (F2-02), normalizados.
    model_base_url_allowlist: tuple[str, ...] = ()

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
            api_key_run=_api_key("API_KEY_RUN", os.getenv("API_KEY_RUN")),
            api_key_admin=_api_key("API_KEY_ADMIN", os.getenv("API_KEY_ADMIN")),
            secrets_dir=_secrets_dir(),
            model_base_url_allowlist=_model_base_url_allowlist(),
        )
        config._validate()
        return config

    def _validate(self) -> None:
        """Valida campos obrigatórios."""
        if not self.mongo_connection_string:
            raise ValueError("MONGO_CONNECTION_STRING é obrigatória")
        if not self.mongo_database_name:
            raise ValueError("MONGO_DATABASE_NAME é obrigatório")
        self._validate_api_keys()

    def _validate_api_keys(self) -> None:
        """As duas chaves juntas (ou nenhuma) e diferentes entre si. Nunca cita o valor."""
        run, admin = self.api_key_run, self.api_key_admin
        if (run is None) != (admin is None):
            missing = "API_KEY_ADMIN" if admin is None else "API_KEY_RUN"
            raise ValueError(
                f"{missing} ausente: defina API_KEY_RUN e API_KEY_ADMIN juntas "
                f"(gere cada uma com: {API_KEY_HINT})"
            )
        if run is not None and run == admin:
            raise ValueError("API_KEY_RUN e API_KEY_ADMIN precisam ser diferentes")


def _environment() -> str:
    """``ENVIRONMENT`` normalizado; vazio = development, fora da lista = erro."""
    value = (os.getenv("ENVIRONMENT") or "").strip().lower() or "development"
    if value not in ENVIRONMENTS:
        raise ValueError(
            f"ENVIRONMENT inválido: {value!r}. Use um de: {', '.join(ENVIRONMENTS)}"
        )
    return value


def _api_key(name: str, raw: Optional[str]) -> Optional[str]:
    """Chave de API do ambiente; vazia = ausente. Erros citam só o nome, nunca o valor."""
    value = (raw or "").strip()
    if not value:
        return None
    if len(value) < API_KEY_MIN_LENGTH:
        raise ValueError(
            f"{name} curta demais: use ao menos {API_KEY_MIN_LENGTH} caracteres (gere com: {API_KEY_HINT})"
        )
    # ASCII visível: o valor vai num header HTTP e é comparado byte a byte.
    if not all("!" <= char <= "~" for char in value):
        raise ValueError(
            f"{name} inválida: use só caracteres ASCII visíveis, sem espaços (gere com: {API_KEY_HINT})"
        )
    return value


def _secrets_dir() -> str:
    """``SECRETS_DIR``: vazio = ``/run/secrets``; relativo ou a raiz ``/`` = erro no startup."""
    value = (os.getenv("SECRETS_DIR") or "").strip()
    if not value:
        return DEFAULT_SECRETS_DIR
    if not os.path.isabs(value):
        raise ValueError("SECRETS_DIR deve ser um caminho absoluto")
    if os.path.normpath(value) in ("/", "//"):
        raise ValueError("SECRETS_DIR não pode ser a raiz do sistema de arquivos")
    return value


def _model_base_url_allowlist() -> tuple[str, ...]:
    """``MODEL_BASE_URL_ALLOWLIST``: hosts separados por vírgula; vazio = nenhum.

    Só o host (nome DNS ou IP literal), sem esquema, porta, caminho nem curinga: a comparação
    é exata com o host da ``base_url``. Normaliza caixa, ponto final e a forma do IPv6.
    """
    raw = os.getenv("MODEL_BASE_URL_ALLOWLIST") or ""
    hosts: dict[str, None] = {}
    for position, entry in enumerate((item.strip() for item in raw.split(",")), start=1):
        if entry:
            hosts[_allowlisted_host(entry, position)] = None
    return tuple(hosts)


def _allowlisted_host(entry: str, position: int) -> str:
    """Host normalizado; inválido = ``ValueError`` com a posição, nunca a entrada (pode ser uma
    URL com credencial colada por engano)."""
    name = normalize_host(entry)
    try:
        ipaddress.ip_address(name)
        return name  # IP literal, já na forma canônica
    except ValueError:
        pass
    if len(name) > 253 or not all(_DNS_LABEL.fullmatch(label) for label in name.split(".")):
        raise ValueError(
            f"MODEL_BASE_URL_ALLOWLIST inválida: entrada {position} (contando da esquerda, separadas por "
            "vírgula) não é um host; use só o host (ex.: llm.interno.example), sem esquema, credencial, "
            "porta, caminho nem '*'"
        )
    return name


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
