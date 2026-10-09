"""Autenticação fail-closed da borda por chave de API (F1-04).

Duas chaves: ``API_KEY_RUN`` (uso dos agentes) e ``API_KEY_ADMIN`` (operação; vale
também onde a run vale). Credencial em ``Authorization: Bearer <chave>`` ou, sem
Bearer, em ``X-API-Key``. Classes de rota (regra default-deny: o que não está nas
tabelas abaixo exige a chave run):

- pública: ``/livez``. O preflight CORS também, mas quem o responde é o
  ``CORSMiddleware`` mais externo, sem chamar a auth (aqui ele não tem exceção: se a
  ordem quebrar, exige chave);
- admin: ``/admin*``, ``/metrics*``, ``/databases*``, ``/eval-runs*``,
  ``/components*``, ``/schedules*``, ``/registry*``, ``/optimize-memories``;
  ``DELETE`` em ``/sessions*`` e ``/memories*``; ``POST|PUT|PATCH|DELETE`` em
  ``/knowledge*``; docs (``/docs*``, ``/redoc``, ``/openapi.json``) fora de development;
- run: todo o resto (inclusive rota desconhecida, que só vira 404 depois da chave).

Modo dev local (sem chaves): não há credencial, mas cada request só passa se
``client``, ``server`` e o header ``Host`` forem loopback (socket UNIX, sem endereço,
não é suportado) e se não vier de outro site no navegador: ``Origin`` presente tem de
estar em ``CORS_ALLOWED_ORIGINS``; sem ``Origin``, ``Sec-Fetch-Site: cross-site`` é
recusado. Isso cobre ``uvicorn --host 0.0.0.0`` direto com ``APP_HOST`` ausente,
``X-Forwarded-For`` forjado (o ``ProxyHeadersMiddleware`` do uvicorn só reescreve
``client``), DNS rebinding e CSRF/CSWSH de uma página qualquer aberta no navegador do
dev. Origem permitida passa mesmo cross-site (os.agno.com -> localhost é sempre
cross-site). Proxy ou túnel rodando no próprio host continua expondo o app.

Respostas: 401 ``{"detail": "unauthorized"}`` + ``WWW-Authenticate: Bearer`` sem
credencial válida; 403 ``{"detail": "forbidden"}`` com a chave run em rota admin.
WebSocket sem credencial suficiente: ``websocket.close`` 1008 antes do accept (pela
especificação ASGI o servidor responde 403 ao handshake). Nada aqui loga header nem chave.

O middleware precisa ficar logo DENTRO do CORS (o preflight é respondido pelo CORS e o
401 de origem permitida leva os headers CORS). Middleware que reescreve o path precisa
ficar fora dele, senão a classificação vê um path diferente do que o roteador vê.
"""

from __future__ import annotations

import hmac
from collections.abc import Collection, Iterable
from dataclasses import dataclass, field
from enum import Enum

from starlette.responses import JSONResponse
from starlette.types import ASGIApp, Receive, Scope, Send

from src.infrastructure.config.app_config import API_KEY_HINT, AppConfig
from src.infrastructure.security.ssrf import is_loopback_host as _is_loopback

# Sem chaves, o app só sobe nestes ambientes e com bind em loopback.
LOCAL_DEV_ENVIRONMENTS = frozenset({"development", "test"})
# RFC 6455, 7.4.1: policy violation.
WS_POLICY_VIOLATION = 1008


class RouteAccess(Enum):
    """Quem pode chamar uma rota (e o papel de uma chave válida)."""

    PUBLIC = "public"
    RUN = "run"
    ADMIN = "admin"


_PUBLIC_PATHS = frozenset({"/livez"})
_ADMIN_PREFIXES = (
    "/admin",
    "/metrics",
    "/databases",
    "/eval-runs",
    "/components",
    "/schedules",
    "/registry",
    "/optimize-memories",
)
_ADMIN_DELETE_PREFIXES = ("/sessions", "/memories")
_ADMIN_WRITE_PREFIXES = ("/knowledge",)
_WRITE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})
_DOCS_PREFIXES = ("/docs", "/redoc", "/openapi.json")


def _under(path: str, prefixes: Iterable[str]) -> bool:
    return any(path == prefix or path.startswith(prefix + "/") for prefix in prefixes)


def classify_route(method: str, path: str, *, docs_require_admin: bool) -> RouteAccess:
    """Classe de acesso de ``method path`` (path de rota, sem root_path nem barra final)."""
    method = method.upper()
    if path in _PUBLIC_PATHS:
        return RouteAccess.PUBLIC
    if _under(path, _ADMIN_PREFIXES):
        return RouteAccess.ADMIN
    if method == "DELETE" and _under(path, _ADMIN_DELETE_PREFIXES):
        return RouteAccess.ADMIN
    if method in _WRITE_METHODS and _under(path, _ADMIN_WRITE_PREFIXES):
        return RouteAccess.ADMIN
    if docs_require_admin and _under(path, _DOCS_PREFIXES):
        return RouteAccess.ADMIN
    return RouteAccess.RUN


@dataclass(frozen=True)
class ApiKeys:
    """Chaves em bytes (comparadas com ``hmac.compare_digest``); fora do repr."""

    run: bytes = field(repr=False)
    admin: bytes = field(repr=False)


def is_local_dev_mode(config: AppConfig) -> bool:
    """Modo dev local (F1-04): sem chaves, ``APP_HOST`` em loopback e ENVIRONMENT development/test.

    Regra única: a borda sobe sem autenticação só nele e o registry de providers (F2-02) só
    aceita ``base_url`` de modelo em loopback nele.
    """
    return (
        config.api_key_run is None
        and config.api_key_admin is None
        and _is_loopback(config.app_host)
        and config.environment in LOCAL_DEV_ENVIRONMENTS
    )


def resolve_api_keys(config: AppConfig) -> ApiKeys | None:
    """Chaves da borda; ``None`` = modo dev local (sem autenticação).

    Sem chaves, só sobe com ``APP_HOST`` em loopback e ``ENVIRONMENT`` development ou
    test; qualquer outro caso levanta ``ValueError``. O ``AppConfig`` já garante que as
    duas chaves vêm juntas e são diferentes.
    """
    if config.api_key_run is not None and config.api_key_admin is not None:
        return ApiKeys(run=config.api_key_run.encode(), admin=config.api_key_admin.encode())
    if is_local_dev_mode(config):
        return None
    raise ValueError(
        "Autenticação obrigatória: defina API_KEY_RUN e API_KEY_ADMIN (gere cada uma com: "
        f"{API_KEY_HINT}). Sem chaves o app só inicia com APP_HOST em loopback e ENVIRONMENT "
        f"development ou test (atual: APP_HOST={config.app_host!r}, ENVIRONMENT={config.environment!r})"
    )


def _route_path(scope: Scope) -> str:
    """Path que o roteador vai casar: sem ``root_path`` (como ``starlette._utils.get_route_path``)
    e sem barra final (como o ``TrailingSlashMiddleware`` do AgentOS)."""
    path: str = scope["path"]
    root_path: str = scope.get("root_path", "")
    if root_path and path.startswith(root_path):
        if path == root_path:
            path = ""
        elif path[len(root_path)] == "/":
            path = path[len(root_path) :]
    return path.rstrip("/") or "/"


def _presented_key(headers: Iterable[tuple[bytes, bytes]]) -> bytes | None:
    """Credencial do request: Bearer tem precedência; ``X-API-Key`` só sem Bearer."""
    authorization: bytes | None = None
    api_key: bytes | None = None
    for name, value in headers:
        if name == b"authorization" and authorization is None:
            authorization = value
        elif name == b"x-api-key" and api_key is None:
            api_key = value
    if authorization is not None:
        scheme, _, token = authorization.strip().partition(b" ")
        if scheme.lower() == b"bearer" and token.strip():
            return token.strip()
    if api_key is not None and api_key.strip():
        return api_key.strip()
    return None


def _host_header_is_loopback(headers: Iterable[tuple[bytes, bytes]]) -> bool:
    """``Host`` em loopback: ``localhost``, ``127.x.y.z`` ou ``[::1]``, porta opcional."""
    raw = next((value for name, value in headers if name == b"host"), None)
    if raw is None:
        return False
    host = raw.decode("latin-1").lower()
    if host.startswith("["):
        end = host.find("]")
        if end == -1:
            return False
        name, rest = host[1 : end], host[end + 1 :]
        if ":" not in name:
            return False
    else:
        name, separator, port = host.partition(":")
        rest = separator + port
    port_digits = rest[1:]
    if rest and not (rest.startswith(":") and port_digits.isascii() and port_digits.isdigit()):
        return False
    return _is_loopback(name)


def _is_local_request(scope: Scope) -> bool:
    """Cliente, servidor e ``Host`` em loopback (sem endereço = recusado)."""
    client = scope.get("client")
    if client is None or not _is_loopback(str(client[0])):
        return False
    server = scope.get("server")
    if server is None or not _is_loopback(str(server[0])):
        return False
    return _host_header_is_loopback(scope["headers"])


def _is_same_site_or_allowed(headers: Iterable[tuple[bytes, bytes]], allowed_origins: frozenset[str]) -> bool:
    """Request de navegador vindo de outro site só passa com ``Origin`` permitido."""
    origin: bytes | None = None
    fetch_site: bytes | None = None
    for name, value in headers:
        if name == b"origin" and origin is None:
            origin = value
        elif name == b"sec-fetch-site" and fetch_site is None:
            fetch_site = value
    if origin is not None:
        return origin.decode("latin-1") in allowed_origins
    return fetch_site is None or fetch_site.strip().lower() != b"cross-site"


class ApiKeyAuthMiddleware:
    """Middleware ASGI puro (``http`` e ``websocket``).

    ``keys=None`` = modo dev local: sem credencial, mas só para request local (ver o
    docstring do módulo).
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        keys: ApiKeys | None,
        docs_require_admin: bool,
        allowed_origins: Collection[str] = (),
    ) -> None:
        self.app = app
        self._keys = keys
        self._docs_require_admin = docs_require_admin
        # Só no modo dev local; vazio = nenhum Origin passa.
        self._allowed_origins = frozenset(allowed_origins)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] not in ("http", "websocket"):
            await self.app(scope, receive, send)
            return
        is_websocket = scope["type"] == "websocket"
        method = "GET" if is_websocket else scope["method"]
        access = classify_route(method, _route_path(scope), docs_require_admin=self._docs_require_admin)
        if access is RouteAccess.PUBLIC:
            await self.app(scope, receive, send)
            return
        if self._keys is None:
            allowed = _is_local_request(scope) and _is_same_site_or_allowed(
                scope["headers"], self._allowed_origins
            )
            role = None
        else:
            role = self._role(_presented_key(scope["headers"]), self._keys)
            allowed = role is RouteAccess.ADMIN or role is access
        if allowed:
            await self.app(scope, receive, send)
            return
        if is_websocket:
            await _reject_websocket(receive, send)
            return
        if role is None:
            response = JSONResponse({"detail": "unauthorized"}, status_code=401, headers={"WWW-Authenticate": "Bearer"})
        else:
            response = JSONResponse({"detail": "forbidden"}, status_code=403)
        await response(scope, receive, send)

    @staticmethod
    def _role(presented: bytes | None, keys: ApiKeys) -> RouteAccess | None:
        if presented is None:
            return None
        # Compara com as duas sempre: o tempo não diz qual chave quase bateu.
        is_admin = hmac.compare_digest(presented, keys.admin)
        is_run = hmac.compare_digest(presented, keys.run)
        if is_admin:
            return RouteAccess.ADMIN
        if is_run:
            return RouteAccess.RUN
        return None


async def _reject_websocket(receive: Receive, send: Send) -> None:
    """Recusa o handshake: ``websocket.close`` antes do accept (o servidor responde 403)."""
    message = await receive()
    if message["type"] == "websocket.connect":
        await send({"type": "websocket.close", "code": WS_POLICY_VIOLATION, "reason": ""})
