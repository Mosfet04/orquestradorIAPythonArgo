"""Rede falsa do httpcore para o transporte guardado (F4-01): nada abre socket nem resolve nome.

``RecordingAsyncBackend``/``RecordingSyncBackend`` ficam no lugar do backend de rede do pool do httpcore
(o que abre o TCP): gravam em ``connects`` o ``(host, porta)`` pedido (o IP conferido pela guarda), em
``server_hostnames`` o nome do TLS (SNI) e em ``written`` os bytes enviados, e respondem com ``response``
(HTTP/1.1 cru). ``refuse``: IPs cujo connect falha com ``httpcore.ConnectError``.
"""

from __future__ import annotations

import ssl
import typing

import httpcore


def http_response(status: int = 200, body: bytes = b'{"ok":1}', headers: dict[str, str] | None = None) -> bytes:
    """Resposta HTTP/1.1 crua com ``Content-Length`` e ``Connection: close`` (cada request abre um connect)."""
    lines = [f"HTTP/1.1 {status} X", "Content-Type: application/json", "Connection: close"]
    lines.append(f"Content-Length: {len(body)}")
    lines += [f"{name}: {value}" for name, value in (headers or {}).items()]
    return ("\r\n".join(lines) + "\r\n\r\n").encode() + body


OK_RESPONSE = http_response()


class _Recorder:
    def __init__(self, response: bytes, refuse: tuple[str, ...]) -> None:
        self.response = response
        self.refuse = refuse
        self.connects: list[tuple[str, int]] = []
        self.server_hostnames: list[str | None] = []
        self.written: list[bytes] = []

    def _connect(self, host: str, port: int) -> None:
        self.connects.append((host, port))
        if host in self.refuse:
            raise httpcore.ConnectError("connect recusado pelo teste")

    def sent(self) -> bytes:
        return b"".join(self.written)


class _AsyncStream(httpcore.AsyncNetworkStream):
    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder
        self._buffer = [recorder.response]
        self._closed = False

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ASYNC109 - assinatura do httpcore
        return self._buffer.pop(0) if self._buffer else b""

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:  # noqa: ASYNC109 - assinatura do httpcore
        self._recorder.written.append(buffer)

    async def aclose(self) -> None:
        self._closed = True

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109 - assinatura do httpcore
    ) -> httpcore.AsyncNetworkStream:
        self._recorder.server_hostnames.append(server_hostname)
        return self

    def get_extra_info(self, info: str) -> typing.Any:
        return None


class RecordingAsyncBackend(_Recorder, httpcore.AsyncNetworkBackend):
    """Backend async falso: grava o connect e responde ``response``."""

    def __init__(self, response: bytes = OK_RESPONSE, refuse: tuple[str, ...] = ()) -> None:
        _Recorder.__init__(self, response, refuse)

    async def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,  # noqa: ASYNC109 - assinatura do httpcore
        local_address: str | None = None,
        socket_options: typing.Iterable[typing.Any] | None = None,
    ) -> httpcore.AsyncNetworkStream:
        self._connect(host, port)
        return _AsyncStream(self)

    async def sleep(self, seconds: float) -> None:
        return None


class _SyncStream(httpcore.NetworkStream):
    def __init__(self, recorder: _Recorder) -> None:
        self._recorder = recorder
        self._buffer = [recorder.response]

    def read(self, max_bytes: int, timeout: float | None = None) -> bytes:
        return self._buffer.pop(0) if self._buffer else b""

    def write(self, buffer: bytes, timeout: float | None = None) -> None:
        self._recorder.written.append(buffer)

    def close(self) -> None:
        return None

    def start_tls(
        self, ssl_context: ssl.SSLContext, server_hostname: str | None = None, timeout: float | None = None
    ) -> httpcore.NetworkStream:
        self._recorder.server_hostnames.append(server_hostname)
        return self

    def get_extra_info(self, info: str) -> typing.Any:
        return None


class RecordingSyncBackend(_Recorder, httpcore.NetworkBackend):
    """Backend síncrono falso: grava o connect e responde ``response``."""

    def __init__(self, response: bytes = OK_RESPONSE, refuse: tuple[str, ...] = ()) -> None:
        _Recorder.__init__(self, response, refuse)

    def connect_tcp(
        self,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: typing.Iterable[typing.Any] | None = None,
    ) -> httpcore.NetworkStream:
        self._connect(host, port)
        return _SyncStream(self)

    def sleep(self, seconds: float) -> None:
        return None
