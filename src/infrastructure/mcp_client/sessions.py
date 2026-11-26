"""Sessões MCP só por streamable HTTP (F4-02): allowlist com hash de schema fixado e ciclo de vida no app.

Uma sessão por servidor (``Tool`` com ``mcp_tools``, chave = id da tool), aberta na primeira montagem de um
agente que a usa (no startup, dentro do lifespan) e fechada em ``aclose`` (``cleanup`` do container, no shutdown
do lifespan). Nada de stdio nem processo local: o SDK só recebe a URL da ``route`` e um ``httpx.AsyncClient``
montado aqui sobre ``transport_factory`` (no app, o ``GuardedAsyncTransport`` com a regra das tools HTTP:
destino conferido a cada conexão e TCP no IP conferido, sem proxy do ambiente e sem cookie guardado). Redirect:
o SDK segue só o da mesma origem (sem consultar ``follow_redirects``) e a guarda confere o destino de novo.
Headers por referência (``env:``/``file:``) são resolvidos ao abrir a sessão: rotação vale na reconexão.

Memória: cada resposta do servidor passa por ``_LimitedTransport`` (entre o cliente e ``transport_factory``):
corpo cru até ``max_response_bytes`` (o SDK lê JSON e SSE sem teto), sem compressão (o cliente pede
``identity``; outro ``Content-Encoding`` é recusado, porque a descompressão do httpx não tem teto) e sem o
stream GET do servidor (só traria notificações, ignoradas aqui: responde 405 sem sair da máquina). Resposta
recusada marca a sessão como quebrada: o pedido pendente é solto na hora (o SDK só o soltaria no prazo).
``tools/list`` guarda só as tools da allowlist.

Allowlist: só as tools de ``mcp_tools`` chegam ao agente, e cada uma só se o ``schema_hash`` do que o servidor
lista (nome, descrição e ``inputSchema``: o que vai ao modelo) for o fixado na config. A lista é conferida a cada
montagem e a cada reconexão; schema divergente recusa a tool (log com o hash esperado e o observado, nunca o
schema) e a chamada dela, mesmo a de um agente já montado. ``instructions`` do servidor (``initialize``) não vão
ao modelo: texto do servidor só passa pelo hash.

O SDK (anyio) exige entrar e sair do contexto da sessão na mesma task: cada sessão tem uma task dona, e as
chamadas de outras tasks vão pelo ``ClientSession`` dela. Falha numa chamada fecha a sessão; a chamada seguinte
reabre (e confere o hash de novo). Config do servidor que muda (``route``/``headers``) fecha a sessão antiga, e a
tool de um agente montado com a config antiga passa a ser recusada.

Conferido no ``mcp`` 1.30.0: ``mcp/client/streamable_http.py`` (``streamable_http_client(url, http_client=,
terminate_on_close=True)``: o cliente passado não é fechado pelo SDK, e o DELETE da sessão sai no fechamento) e
``mcp/client/session.py`` (``initialize``, ``list_tools(params=)``, ``call_tool``); pedido pendente numa sessão que
cai recebe ``McpError`` "Connection closed" (``mcp/shared/session.py``).
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from collections.abc import AsyncIterator, Callable, Collection, Coroutine, Mapping
from dataclasses import dataclass, field
from datetime import timedelta
from typing import TYPE_CHECKING, Any, TypeVar, cast
from urllib.parse import urlsplit

import httpx

from src.domain.entities.tool import Tool
from src.domain.ports import ILogger
from src.infrastructure.config.secrets import DEFAULT_SECRETS_DIR, UnresolvedHeaderError, resolve_header_refs
from src.infrastructure.security.guarded_http import cookieless_jar
from src.infrastructure.security.ssrf import BlockedDestinationError

if TYPE_CHECKING:
    from mcp import ClientSession
    from mcp.types import CallToolResult

T = TypeVar("T")

MCP_EXTRA_HINT = "pip install --require-hashes -r requirements.lock -r requirements-mcp.lock"
DEFAULT_TIMEOUT_SECONDS = 30.0
"""Prazo de abrir a sessão (``initialize``), de cada operação HTTP e de cada chamada de tool."""
DEFAULT_STREAM_READ_SECONDS = 300.0
"""Leitura do stream SSE do servidor (o mesmo default do SDK)."""
DEFAULT_MAX_RESULT_CHARS = 1024 * 1024
"""Texto do resultado que vai ao modelo; acima disso, erro curto (como o corpo das tools HTTP)."""
DEFAULT_MAX_RESPONSE_BYTES = 4 * 1024 * 1024
"""Corpo cru de cada resposta HTTP do servidor (JSON-RPC escapado: 1 Mi caracteres cabem com folga)."""
MAX_LIST_PAGES = 50
"""Páginas de ``tools/list`` lidas por servidor (servidor que pagina sem fim não segura a montagem)."""
CLOSE_TIMEOUT_SECONDS = 5.0
_HTTP_SCHEMES = frozenset({"http", "https"})
_CLOSED = "cliente MCP já fechado (shutdown)"
_DIVERGENT = "schema do servidor diverge do hash fixado"
_DIVERGENT_LOG = "Tool MCP recusada: schema do servidor diverge do hash fixado"


def schema_hash(name: str, description: str | None, input_schema: Mapping[str, Any]) -> str:
    """SHA-256 (hex) do JSON canônico de ``name``, ``description`` e ``inputSchema`` (o valor de ``mcp_tools``).

    Canônico: chaves ordenadas, sem espaço, UTF-8. Descrição ausente vale ``""``.
    """
    canonical = json.dumps(
        {"name": name, "description": description or "", "inputSchema": input_schema},
        sort_keys=True,
        separators=(",", ":"),
        ensure_ascii=False,
        allow_nan=False,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class PinnedTool:
    """Tool do servidor como o modelo a vê, com o hash conferido contra a allowlist."""

    name: str
    description: str
    input_schema: dict[str, Any]
    schema_hash: str = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, "schema_hash", schema_hash(self.name, self.description, self.input_schema))


class McpServerError(Exception):
    """Servidor MCP que não abriu: ``reason`` é texto nosso (sem host, header nem texto do SDK)."""

    def __init__(self, reason: str, error_type: str | None = None) -> None:
        super().__init__(reason)
        self.reason = reason
        self.error_type = error_type or "McpServerError"


class ResponseTooLargeError(Exception):
    """Resposta do servidor MCP acima de ``max_response_bytes``."""


class UnsupportedContentEncodingError(Exception):
    """Resposta do servidor MCP comprimida (o cliente pede ``identity``)."""


class _SessionLost(Exception):
    """A sessão caiu com o pedido pendente (o SDK não responde ao pedido: só o prazo o soltaria)."""

    def __init__(self, error_type: str) -> None:
        super().__init__("sessão MCP encerrada")
        self.error_type = error_type


def _error_type(exc: BaseException) -> str:
    return exc.error_type if isinstance(exc, _SessionLost) else type(_leaf(exc)).__name__


class _ConfigChanged(Exception):
    """A sessão do id é de outra config (``route``/``headers``): a tool do agente antigo é recusada."""


def _fingerprint(tool: Tool) -> tuple[str, tuple[tuple[str, str], ...]]:
    return tool.route, tuple(sorted((tool.headers or {}).items()))


def _leaf(exc: BaseException) -> BaseException:
    """Primeira exceção folha de um ``ExceptionGroup`` (o anyio agrupa as falhas das tasks do SDK)."""
    while isinstance(exc, BaseExceptionGroup) and exc.exceptions:
        exc = exc.exceptions[0]
    return exc


def _contains(exc: BaseException, kind: type[BaseException]) -> bool:
    if isinstance(exc, BaseExceptionGroup):
        return any(_contains(inner, kind) for inner in exc.exceptions)
    return isinstance(exc, kind)


def _open_failure(exc: BaseException) -> McpServerError:
    if _contains(exc, BlockedDestinationError):
        return McpServerError("destino do servidor MCP recusado pela guarda SSRF", "BlockedDestinationError")
    if _contains(exc, TimeoutError):
        return McpServerError("servidor MCP sem resposta no prazo", "TimeoutError")
    error_type = _error_type(exc)
    return McpServerError(f"servidor MCP indisponível ({error_type})", error_type)


@dataclass(frozen=True)
class _Sdk:
    """O que a sessão usa do SDK ``mcp`` (extra opcional), importado só ao abrir a primeira sessão."""

    client_session: type[ClientSession]
    streamable_http_client: Any  # context manager async do SDK (``mcp.client.streamable_http``)


class _LimitedStream(httpx.AsyncByteStream):
    """Corpo cru da resposta, até ``limit`` bytes; acima disso, ``refuse`` e erro (a leitura para ali)."""

    def __init__(
        self, inner: httpx.AsyncByteStream, limit: int, refuse: Callable[[Exception], Exception]
    ) -> None:
        self._inner = inner
        self._limit = limit
        self._refuse = refuse

    async def __aiter__(self) -> AsyncIterator[bytes]:
        total = 0
        async for chunk in self._inner:
            total += len(chunk)
            if total > self._limit:
                raise self._refuse(ResponseTooLargeError(f"resposta do servidor MCP acima de {self._limit} bytes"))
            yield chunk

    async def aclose(self) -> None:
        await self._inner.aclose()


class _LimitedTransport(httpx.AsyncBaseTransport):
    """Transporte da sessão sobre o de ``transport_factory``; ver o docstring do módulo (Memória)."""

    def __init__(
        self, inner: httpx.AsyncBaseTransport, limit: int, on_refused: Callable[[Exception], None]
    ) -> None:
        self._inner = inner
        self._limit = limit
        self._on_refused = on_refused

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET":  # stream do servidor: só notificações, ignoradas aqui
            return httpx.Response(405, request=request)
        response = await self._inner.handle_async_request(request)
        if response.headers.get("Content-Encoding", "identity").strip().lower() != "identity":
            await response.aclose()
            raise self._refuse(UnsupportedContentEncodingError("resposta do servidor MCP comprimida"))
        response.stream = _LimitedStream(cast(httpx.AsyncByteStream, response.stream), self._limit, self._refuse)
        return response

    async def aclose(self) -> None:
        await self._inner.aclose()

    def _refuse(self, exc: Exception) -> Exception:
        self._on_refused(exc)
        return exc


def _import_sdk() -> _Sdk:
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import streamable_http_client
    except ImportError:
        raise McpServerError(f"SDK mcp não instalado: instale o extra ({MCP_EXTRA_HINT})", "ImportError") from None
    return _Sdk(client_session=ClientSession, streamable_http_client=streamable_http_client)


class _Connection:
    """Sessão MCP com task dona (entra e sai do contexto do SDK na mesma task)."""

    def __init__(self, tool_id: str, fingerprint: tuple[str, tuple[tuple[str, str], ...]], logger: ILogger) -> None:
        self.tool_id = tool_id
        self.fingerprint = fingerprint
        self.listed: dict[str, PinnedTool | None] = {}
        self._logger = logger
        self._session: ClientSession | None = None
        self._task: asyncio.Task[None] | None = None
        self._ready: asyncio.Future[ClientSession] | None = None
        self._stop = asyncio.Event()
        self._broken = asyncio.Event()
        self._failure_type = "ConnectionError"

    @property
    def alive(self) -> bool:
        return (
            self._session is not None
            and self._task is not None
            and not self._task.done()
            and not self._broken.is_set()
        )

    def refused(self, exc: Exception) -> None:
        """Resposta recusada pelo ``_LimitedTransport``: a sessão não serve mais e quem espera é solto."""
        self._failure_type = type(exc).__name__
        self._broken.set()
        if self._ready is not None and not self._ready.done() and not self._stop.is_set():
            self._ready.set_exception(exc)

    @property
    def session(self) -> ClientSession:
        if self._session is None:
            raise RuntimeError("sessão MCP não aberta")
        return self._session

    async def open(self, sdk: _Sdk, url: str, client: httpx.AsyncClient, timeout: float) -> None:  # noqa: ASYNC109 - prazo do handshake
        ready: asyncio.Future[ClientSession] = asyncio.get_running_loop().create_future()
        self._ready = ready
        self._task = asyncio.create_task(self._run(sdk, url, client, timeout, ready), name=f"mcp-{self.tool_id}")
        async with asyncio.timeout(timeout):
            self._session = await asyncio.shield(ready)

    async def _run(
        self,
        sdk: _Sdk,
        url: str,
        client: httpx.AsyncClient,
        timeout: float,  # noqa: ASYNC109 - prazo de cada pedido da sessão
        ready: asyncio.Future[ClientSession],
    ) -> None:
        try:
            async with (
                client,
                sdk.streamable_http_client(url, http_client=client) as (read, write, _),
                sdk.client_session(read, write, read_timeout_seconds=timedelta(seconds=timeout)) as session,
            ):
                await session.initialize()
                if not ready.done():  # o initialize não volta se a resposta dele foi recusada (``refused``)
                    ready.set_result(session)
                await self._stop.wait()
        except Exception as exc:  # noqa: BLE001 - vai a quem espera a abertura, ou ao log se a sessão caiu
            if self._stop.is_set():  # fechada por nós (inclusive a abertura que estourou o prazo): nada a avisar
                return
            if not ready.done():
                ready.set_exception(exc)
                return
            self._failure_type = type(_leaf(exc)).__name__
            self._logger.warning(
                "Sessão MCP encerrada pelo servidor ou pela rede", tool_id=self.tool_id, error_type=self._failure_type
            )

    async def run(self, operation: Coroutine[Any, Any, T], timeout: float) -> T:  # noqa: ASYNC109 - prazo do pedido
        """``operation`` na sessão, solta se a sessão cair antes da resposta (``_SessionLost``) ou no prazo."""
        if self._task is None:
            operation.close()
            raise _SessionLost(self._failure_type)
        pending = asyncio.ensure_future(operation)
        broken = asyncio.ensure_future(self._broken.wait())
        try:
            done, _ = await asyncio.wait(
                {pending, self._task, broken}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        except asyncio.CancelledError:  # quem chamou foi cancelado: o pedido não fica órfão
            pending.cancel()
            raise
        finally:
            broken.cancel()
        if pending in done:
            return pending.result()
        pending.cancel()
        await asyncio.wait({pending})
        if self._task.done() or self._broken.is_set():
            raise _SessionLost(self._failure_type)
        raise TimeoutError

    async def close(self) -> None:
        """Encerra a sessão (DELETE ao servidor, se ele deu id) e espera a task; idempotente."""
        self._stop.set()
        task = self._task
        if task is None:
            return
        # Resposta do initialize recusada (``refused``): o SDK não o solta antes do prazo, esperar não adianta.
        stuck = self._broken.is_set() and self._session is None
        done = set() if stuck else (await asyncio.wait({task}, timeout=CLOSE_TIMEOUT_SECONDS))[0]
        if not done:
            task.cancel()
            await asyncio.wait({task})
        self._session = None


class McpSessions:
    """Sessões dos servidores MCP do app; ver o docstring do módulo."""

    def __init__(
        self,
        logger: ILogger,
        *,
        transport_factory: Callable[[], httpx.AsyncBaseTransport],
        secrets_dir: str = DEFAULT_SECRETS_DIR,
        timeout: float = DEFAULT_TIMEOUT_SECONDS,
        stream_read_timeout: float = DEFAULT_STREAM_READ_SECONDS,
        max_result_chars: int = DEFAULT_MAX_RESULT_CHARS,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
    ) -> None:
        """``transport_factory``: transporte novo por sessão (pode ler disco: é chamado em thread)."""
        self._logger = logger
        self._transport_factory = transport_factory
        self._secrets_dir = secrets_dir
        self._timeout = timeout
        self._stream_read_timeout = stream_read_timeout
        self._max_result_chars = max_result_chars
        self._max_response_bytes = max_response_bytes
        self._connections: dict[str, _Connection] = {}
        self._locks: dict[str, asyncio.Lock] = {}
        self._closed = False

    async def pinned_tools(self, tool: Tool) -> list[PinnedTool]:
        """Tools da allowlist que o servidor lista com o hash fixado; lista de novo a cada montagem.

        ``McpServerError`` se o servidor não abre (SDK ausente, header que não resolve, destino recusado,
        rede, prazo). Tool ausente ou divergente fica de fora, com log.
        """
        async with self._lock(tool.id):
            existing = self._connections.get(tool.id)
            connection = await self._current(tool, from_call=False)
            if connection is existing:  # sessão nova já listou ao abrir (``_open``); a existente lista de novo
                try:
                    connection.listed = await connection.run(self._list(connection.session, tool), self._timeout)
                except Exception as exc:  # noqa: BLE001 - vira McpServerError (texto nosso), logado por quem monta
                    await self._drop(tool.id, connection)
                    raise _open_failure(exc) from None
        pinned: list[PinnedTool] = []
        for name, expected in (tool.mcp_tools or {}).items():
            listed = self._verified(tool, name, expected, connection)
            if listed is not None:
                pinned.append(listed)
        return pinned

    async def call(self, tool: Tool, name: str, arguments: dict[str, Any]) -> str:
        """Chama ``name`` no servidor e devolve o texto para o modelo; toda falha vira erro curto, com log."""
        label = f"Erro na tool MCP {tool.id}/{name}"
        if self._closed:
            return f"{label}: {_CLOSED}"
        expected = (tool.mcp_tools or {}).get(name)
        if expected is None:
            return f"{label}: tool fora da allowlist"
        try:
            async with self._lock(tool.id):
                connection = await self._current(tool, from_call=True)
        except _ConfigChanged:
            return f"{label}: configuração do servidor MCP mudou; tente de novo"
        except McpServerError as exc:
            self._logger.error(
                "Servidor MCP indisponível; a chamada não saiu",
                tool_id=tool.id,
                mcp_tool=name,
                reason=exc.reason,
                error_type=exc.error_type,
            )
            return f"{label}: {exc.reason}"
        if self._verified(tool, name, expected, connection) is None:
            return f"{label}: {_DIVERGENT}"
        try:
            result = await connection.run(connection.session.call_tool(name, arguments), self._timeout)
        except Exception as exc:  # noqa: BLE001 - erro curto ao modelo; sessão reaberta na próxima chamada
            error_type = _error_type(exc)
            self._logger.warning(
                "Falha na chamada da tool MCP; a sessão será reaberta",
                tool_id=tool.id,
                mcp_tool=name,
                error_type=error_type,
            )
            await self._drop(tool.id, connection)
            return f"{label}: falha na chamada ({error_type})"
        return self._result_text(label, result)

    async def aclose(self) -> None:
        """Fecha todas as sessões em paralelo (shutdown: cada uma espera até 5 s); idempotente.

        Depois dele nada abre nem chama.
        """
        self._closed = True
        connections, self._connections = list(self._connections.values()), {}
        results = await asyncio.gather(*(c.close() for c in connections), return_exceptions=True)
        for connection, result in zip(connections, results, strict=True):
            if isinstance(result, Exception):
                self._logger.error(
                    "Falha ao fechar a sessão MCP", tool_id=connection.tool_id, error_type=type(result).__name__
                )
            elif isinstance(result, BaseException):
                raise result

    # ── private ─────────────────────────────────────────────────────

    def _is_closed(self) -> bool:
        """``_closed`` relido depois de um ``await`` (o mypy estreita o atributo pelo ``if`` anterior)."""
        return self._closed

    def _lock(self, tool_id: str) -> asyncio.Lock:
        return self._locks.setdefault(tool_id, asyncio.Lock())

    async def _current(self, tool: Tool, *, from_call: bool) -> _Connection:
        """Sessão viva do servidor da tool; reabre a que caiu. Chame com a trava do id."""
        if self._closed:
            raise McpServerError(_CLOSED)
        current = self._connections.get(tool.id)
        if current is not None and current.fingerprint != _fingerprint(tool):
            if from_call:
                raise _ConfigChanged
            await self._drop(tool.id, current)
            current = None
        if current is not None and current.alive:
            return current
        if current is not None:
            await self._drop(tool.id, current)
        connection = await self._open(tool)
        if self._is_closed():  # aclose durante a abertura
            await connection.close()
            raise McpServerError(_CLOSED)
        self._connections[tool.id] = connection
        return connection

    async def _drop(self, tool_id: str, connection: _Connection) -> None:
        if self._connections.get(tool_id) is connection:
            del self._connections[tool_id]
        await connection.close()

    async def _open(self, tool: Tool) -> _Connection:
        if urlsplit(tool.route).scheme.lower() not in _HTTP_SCHEMES:
            raise McpServerError("route do servidor MCP deve ser http(s): stdio e processo local não são aceitos")
        sdk = _import_sdk()
        try:
            headers = await resolve_header_refs(tool.headers or {}, self._secrets_dir)
        except UnresolvedHeaderError as exc:
            raise McpServerError(f"header por referência não resolvido ({exc.name})", exc.error_type) from None
        connection = _Connection(tool.id, _fingerprint(tool), self._logger)
        transport = _LimitedTransport(
            await asyncio.to_thread(self._transport_factory), self._max_response_bytes, connection.refused
        )
        client_headers = httpx.Headers(headers)
        client_headers["Accept-Encoding"] = "identity"
        client = httpx.AsyncClient(
            transport=transport,
            headers=client_headers,
            timeout=httpx.Timeout(self._timeout, read=self._stream_read_timeout),
            follow_redirects=False,
            cookies=cookieless_jar(),
        )
        try:
            await connection.open(sdk, tool.route, client, self._timeout)
            connection.listed = await connection.run(self._list(connection.session, tool), self._timeout)
        except BaseException as exc:  # cancelamento também: a task dona não pode sobrar fora do registro
            await asyncio.shield(self._discard(connection, client))
            if not isinstance(exc, Exception):
                raise
            raise _open_failure(exc) from None
        self._logger.info("Sessão MCP aberta", tool_id=tool.id, listed_count=len(connection.listed))
        return connection

    @staticmethod
    async def _discard(connection: _Connection, client: httpx.AsyncClient) -> None:
        await connection.close()
        await client.aclose()  # se a task dona nem chegou a abrir o cliente

    @staticmethod
    async def _list(session: ClientSession, tool: Tool) -> dict[str, PinnedTool | None]:
        """``tools/list`` paginado, só com as tools da allowlist.

        Nome repetido no servidor vira ``None`` (recusado: não se sabe qual roda).
        """
        from mcp.types import PaginatedRequestParams

        wanted: Collection[str] = tool.mcp_tools or {}
        listed: dict[str, PinnedTool | None] = {}
        cursor: str | None = None
        for _ in range(MAX_LIST_PAGES):
            page = await session.list_tools(params=PaginatedRequestParams(cursor=cursor) if cursor else None)
            for item in page.tools:
                if item.name not in wanted:
                    continue
                pinned = PinnedTool(item.name, item.description or "", dict(item.inputSchema))
                listed[item.name] = None if item.name in listed else pinned
            cursor = page.nextCursor
            if not cursor:
                break
        return listed

    def _verified(self, tool: Tool, name: str, expected: str, connection: _Connection) -> PinnedTool | None:
        if name not in connection.listed:
            self._logger.error("Tool MCP da allowlist não existe no servidor", tool_id=tool.id, mcp_tool=name)
            return None
        listed = connection.listed[name]
        if listed is None or listed.schema_hash != expected:
            self._logger.error(
                _DIVERGENT_LOG,
                tool_id=tool.id,
                mcp_tool=name,
                expected_hash=expected,
                observed_hash=listed.schema_hash if listed is not None else "nome repetido no servidor",
            )
            return None
        return listed

    def _result_text(self, label: str, result: CallToolResult) -> str:
        from mcp.types import TextContent

        parts = [
            block.text if isinstance(block, TextContent) else f"[conteúdo {block.type} omitido]"
            for block in result.content
        ]
        if not parts and result.structuredContent is not None:
            parts = [json.dumps(result.structuredContent, ensure_ascii=False)]
        text = "\n".join(parts)
        if len(text) > self._max_result_chars:
            return f"{label}: resultado acima do limite ({self._max_result_chars} caracteres)"
        return f"{label}: {text}" if result.isError else text
