"""Transporte HTTP com a guarda SSRF na conexão (F4-01): o TCP abre no IP conferido.

O backend de rede do pool do httpcore (quem abre o TCP) é trocado por um que confere o host pela
``DestinationRule`` (resolve o nome UMA vez, fora do event loop e com prazo) e conecta num dos
endereços conferidos, nunca num nome resolvido de novo: fecha o DNS rebinding. O resto do request
fica como veio: header ``Host`` e SNI/certificado do TLS seguem o nome da URL e o pool continua
separado por nome (dois hosts no mesmo IP não dividem conexão).

Cada conexão nova re-checa o destino (inclusive a de um redirect, se alguém o seguisse); os clientes
daqui não seguem redirect (``follow_redirects=False``) e não usam proxy do ambiente (com proxy, o TCP
iria ao proxy e o destino real escaparia da guarda: o httpx só lê o proxy do ambiente sem
``transport=``, ``httpx/_client.py``, ``allow_env_proxies``).

Conferido no httpx 0.28.1/httpcore 1.0.9: o ``AsyncHTTPTransport``/``HTTPTransport`` guardam o pool só
em ``_pool`` (``httpx/_transports/default.py``) e o pool aceita ``network_backend``
(``httpcore/_async/connection_pool.py``); a conexão chama ``connect_tcp(host, port, ...)`` com o host
da URL e ``start_tls(server_hostname=<host>)`` (``httpcore/_async/connection.py``). Teste de pino:
``tests/golden/test_f4_01_httpx_internals_golden.py``; teto ``httpx<0.29`` no ``requirements.in``.

O DNS do caminho async roda num executor próprio e pequeno (``DNS_MAX_THREADS``), não no padrão do
loop: o ``getaddrinfo`` não é cancelável, e um DNS que não responde prenderia threads do
``asyncio.to_thread`` do app. Com o executor cheio, o pedido seguinte espera na fila e sai por timeout.

Os clientes compartilhados não guardam cookie (``cookieless_jar``): servem a todas as tools, modelos,
agentes e usuários, e um ``Set-Cookie`` guardado iria nas chamadas dos outros.
"""

from __future__ import annotations

import asyncio
import typing
from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
from http.cookiejar import CookieJar, DefaultCookiePolicy

import httpcore
import httpx

from src.infrastructure.security.ssrf import (
    BlockedDestinationError,
    DestinationRule,
    IPAddress,
    Resolver,
    literal_address,
    system_resolver,
)

DNS_TIMEOUT_SECONDS = 5.0
"""Prazo da resolução do host na conexão (o ``getaddrinfo`` do sistema pode levar ~30 s até desistir)."""

DNS_MAX_THREADS = 8
"""Threads do executor de DNS do caminho async (compartilhado pelos transportes guardados do processo)."""

_DNS_EXECUTOR = ThreadPoolExecutor(max_workers=DNS_MAX_THREADS, thread_name_prefix="guarda-dns")

DEFAULT_LIMITS = httpx.Limits(max_connections=100, max_keepalive_connections=20, keepalive_expiry=5.0)
"""Os mesmos do httpx (``httpx/_config.py``, ``DEFAULT_LIMITS``)."""

# O mesmo tipo do httpcore (``httpcore/_backends/base.py``, ``SOCKET_OPTION``).
SocketOptions = Iterable[tuple[int, int, int] | tuple[int, int, bytes | bytearray] | tuple[int, int, None, int]]


class _GuardedAsyncBackend(httpcore.AsyncNetworkBackend):
    """Backend async: confere o host e conecta no primeiro endereço conferido que aceitar."""

    def __init__(
        self, rule: DestinationRule, resolver: Resolver, dns_timeout: float, inner: httpcore.AsyncNetworkBackend
    ) -> None:
        self._rule = rule
        self._resolver = resolver
        self._dns_timeout = dns_timeout
        self._inner = inner

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - assinatura do httpcore
        local_address: str | None = None,
        socket_options: SocketOptions | None = None,
    ) -> httpcore.AsyncNetworkStream:
        addresses = await self._addresses(host, timeout)
        failure: httpcore.ConnectError | httpcore.ConnectTimeout | None = None
        for address in addresses:
            try:
                return await self._inner.connect_tcp(
                    str(address), port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                failure = exc
        raise failure or httpcore.ConnectError("nenhum endereço conferido")

    async def _addresses(self, host: str, timeout: float | None) -> list[IPAddress]:  # noqa: ASYNC109 - assinatura do httpcore
        if literal_address(host) is not None:  # IP literal: sem DNS, sem thread
            return self._rule.addresses(host, self._resolver)
        limit = self._dns_timeout if timeout is None else min(timeout, self._dns_timeout)
        resolving = asyncio.get_running_loop().run_in_executor(
            _DNS_EXECUTOR, self._rule.addresses, host, self._resolver
        )
        try:
            return await asyncio.wait_for(resolving, limit)
        except TimeoutError:
            # O thread do getaddrinfo não é cancelável: termina sozinho, no executor de DNS (o pedido
            # ainda na fila é cancelado); a conexão não sai.
            raise httpcore.ConnectTimeout("DNS do destino sem resposta no prazo") from None

    async def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: SocketOptions | None = None  # noqa: ASYNC109 - assinatura do httpcore
    ) -> httpcore.AsyncNetworkStream:
        raise BlockedDestinationError("socket unix não é destino permitido")

    async def sleep(self, seconds: float) -> None:
        await self._inner.sleep(seconds)


class _GuardedSyncBackend(httpcore.NetworkBackend):
    """Backend síncrono (SDKs que chamam a rede fora do event loop): mesma regra, DNS na própria thread."""

    def __init__(self, rule: DestinationRule, resolver: Resolver, inner: httpcore.NetworkBackend) -> None:
        self._rule = rule
        self._resolver = resolver
        self._inner = inner

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: SocketOptions | None = None,
    ) -> httpcore.NetworkStream:
        failure: httpcore.ConnectError | httpcore.ConnectTimeout | None = None
        for address in self._rule.addresses(host, self._resolver):
            try:
                return self._inner.connect_tcp(
                    str(address), port, timeout=timeout, local_address=local_address, socket_options=socket_options
                )
            except (httpcore.ConnectError, httpcore.ConnectTimeout) as exc:
                failure = exc
        raise failure or httpcore.ConnectError("nenhum endereço conferido")

    def connect_unix_socket(
        self, path: str, timeout: float | None = None, socket_options: SocketOptions | None = None
    ) -> httpcore.NetworkStream:
        raise BlockedDestinationError("socket unix não é destino permitido")

    def sleep(self, seconds: float) -> None:
        self._inner.sleep(seconds)


def cookieless_jar() -> CookieJar:
    """Jar que não guarda nem devolve cookie (política sem domínio permitido), para cliente compartilhado.

    Vai direto no ``cookies=`` do cliente: o httpx usa o jar recebido como está (``httpx/_models.py``,
    ``Cookies.__init__``), e um ``httpx.Cookies`` seria copiado para um jar padrão.
    """
    return CookieJar(policy=DefaultCookiePolicy(allowed_domains=[]))


def _pool_kwargs(limits: httpx.Limits) -> dict[str, typing.Any]:
    # Verificação TLS como a do httpx padrão (certifi ou SSL_CERT_FILE/SSL_CERT_DIR do operador); lê
    # disco: construa o transporte fora do event loop.
    return {
        "ssl_context": httpx.create_ssl_context(),
        "max_connections": limits.max_connections,
        "max_keepalive_connections": limits.max_keepalive_connections,
        "keepalive_expiry": limits.keepalive_expiry,
    }


class GuardedAsyncTransport(httpx.AsyncHTTPTransport):
    """``AsyncHTTPTransport`` do httpx com o pool sobre o backend guardado.

    ``network_backend``: o que abre o TCP no IP conferido (padrão: o anyio do httpcore; testes injetam um
    falso). Não chama o ``__init__`` do pai: ele só monta o ``_pool`` (que aqui é outro) e criaria um
    segundo contexto TLS. Sem proxy.
    """

    def __init__(
        self,
        rule: DestinationRule,
        *,
        resolver: Resolver = system_resolver,
        dns_timeout: float = DNS_TIMEOUT_SECONDS,
        limits: httpx.Limits = DEFAULT_LIMITS,
        network_backend: httpcore.AsyncNetworkBackend | None = None,
    ) -> None:
        backend = _GuardedAsyncBackend(rule, resolver, dns_timeout, network_backend or httpcore.AnyIOBackend())
        self._pool = httpcore.AsyncConnectionPool(network_backend=backend, **_pool_kwargs(limits))


class GuardedSyncTransport(httpx.HTTPTransport):
    """``HTTPTransport`` do httpx com o pool sobre o backend guardado (ver ``GuardedAsyncTransport``)."""

    def __init__(
        self,
        rule: DestinationRule,
        *,
        resolver: Resolver = system_resolver,
        limits: httpx.Limits = DEFAULT_LIMITS,
        network_backend: httpcore.NetworkBackend | None = None,
    ) -> None:
        backend = _GuardedSyncBackend(rule, resolver, network_backend or httpcore.SyncBackend())
        self._pool = httpcore.ConnectionPool(network_backend=backend, **_pool_kwargs(limits))


class GuardedTransport(httpx.AsyncBaseTransport, httpx.BaseTransport):
    """Um transporte para cliente síncrono e assíncrono (SDK que passa os mesmos kwargs aos dois, como o ``ollama``)."""

    def __init__(self, async_transport: GuardedAsyncTransport, sync_transport: GuardedSyncTransport) -> None:
        self._async = async_transport
        self._sync = sync_transport

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        """Request de ``httpx.AsyncClient``: pelo transporte async guardado."""
        return await self._async.handle_async_request(request)

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Request de ``httpx.Client``: pelo transporte síncrono guardado."""
        return self._sync.handle_request(request)

    async def aclose(self) -> None:
        """Fecha o pool async (o ``httpx.AsyncClient`` chama no ``aclose``)."""
        await self._async.aclose()

    def close(self) -> None:
        """Fecha o pool síncrono (o ``httpx.Client`` chama no ``close``)."""
        self._sync.close()


class GuardedHttp:
    """Transporte e clientes httpx guardados, compartilhados pelos SDKs de modelo (``base_url`` da config).

    Criados na primeira vez que alguém pede (construção com I/O de disco: chame fora do event loop) e
    fechados em ``aclose`` (shutdown do app). Clientes sem redirect, sem proxy do ambiente e sem cookie
    guardado; timeout e retries ficam com o SDK, que os passa por request.
    """

    def __init__(
        self,
        rule: DestinationRule,
        *,
        resolver: Resolver = system_resolver,
        dns_timeout: float = DNS_TIMEOUT_SECONDS,
        async_backend: httpcore.AsyncNetworkBackend | None = None,
        sync_backend: httpcore.NetworkBackend | None = None,
    ) -> None:
        self._rule = rule
        self._resolver = resolver
        self._dns_timeout = dns_timeout
        self._async_backend = async_backend
        self._sync_backend = sync_backend
        self._transport: GuardedTransport | None = None
        self._async_client: httpx.AsyncClient | None = None
        self._sync_client: httpx.Client | None = None

    def transport(self) -> GuardedTransport:
        """Transporte duplo (para ``transport=`` de um SDK que monta o próprio cliente)."""
        if self._transport is None:
            self._transport = GuardedTransport(
                GuardedAsyncTransport(
                    self._rule,
                    resolver=self._resolver,
                    dns_timeout=self._dns_timeout,
                    network_backend=self._async_backend,
                ),
                GuardedSyncTransport(self._rule, resolver=self._resolver, network_backend=self._sync_backend),
            )
        return self._transport

    def async_client(self) -> httpx.AsyncClient:
        """``httpx.AsyncClient`` guardado (para ``http_client=`` de SDK async)."""
        if self._async_client is None:
            self._async_client = httpx.AsyncClient(
                transport=self.transport(), follow_redirects=False, cookies=cookieless_jar()
            )
        return self._async_client

    def sync_client(self) -> httpx.Client:
        """``httpx.Client`` guardado (para ``http_client=`` de SDK síncrono)."""
        if self._sync_client is None:
            self._sync_client = httpx.Client(
                transport=self.transport(), follow_redirects=False, cookies=cookieless_jar()
            )
        return self._sync_client

    async def aclose(self) -> None:
        """Fecha clientes e transporte criados; idempotente."""
        async_client, self._async_client = self._async_client, None
        sync_client, self._sync_client = self._sync_client, None
        transport, self._transport = self._transport, None
        if async_client is not None:
            await async_client.aclose()
        if sync_client is not None:
            sync_client.close()
        if transport is not None:
            await transport.aclose()
            transport.close()
