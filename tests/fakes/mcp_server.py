"""Servidor MCP falso em processo (F4-02): o SDK ``mcp`` de verdade, sem socket nem rede.

``FakeMcpServer`` monta um servidor de baixo nível do SDK (``mcp.server.lowlevel.Server``) atrás do
``StreamableHTTPSessionManager`` (streamable HTTP, respostas JSON) e o expõe por ``transport()``: um
``httpx.ASGITransport`` que grava os requests recebidos (método, caminho, headers) em ``requests``.

``tools``: o que o ``tools/list`` devolve (troque a lista para simular schema que mudou no servidor);
``results``: resultado por nome de tool (``CallToolResult`` ou texto); ``calls``: ``(nome, argumentos)``
de cada ``tools/call``; ``fail_posts``: quantos POSTs seguintes respondem 500 (queda do servidor);
``delays``: espera antes de responder, por método JSON-RPC (``initialize`` lento...) ou HTTP (``DELETE``);
``chunk_size``: corpo das respostas em pedaços desse tamanho, com ``bytes_read`` = bytes que o cliente leu
(para medir um teto de leitura); ``gzip``: corpo comprimido com ``Content-Encoding: gzip``.
``stateless=False`` usa sessão com ``mcp-session-id`` (o cliente encerra com DELETE ao fechar). Use como
contexto async: o gerenciador de sessões do SDK roda enquanto ele estiver aberto.
"""

from __future__ import annotations

import asyncio
import gzip
import json
from collections.abc import AsyncIterator
from typing import Any

import httpx
from mcp import types
from mcp.server.lowlevel import Server
from mcp.server.streamable_http_manager import StreamableHTTPSessionManager

FAKE_MCP_URL = "https://mcp.example.com/mcp"


def mcp_tool(name: str, description: str = "Descrição", properties: dict[str, Any] | None = None) -> types.Tool:
    """Tool do ``tools/list`` com um ``inputSchema`` de objeto."""
    return types.Tool(
        name=name,
        description=description,
        inputSchema={"type": "object", "properties": properties or {"q": {"type": "string"}}},
    )


class _CountedStream(httpx.AsyncByteStream):
    def __init__(self, body: bytes, server: FakeMcpServer, chunk_size: int) -> None:
        self._body = body
        self._server = server
        self._chunk_size = chunk_size

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for start in range(0, len(self._body), self._chunk_size):
            chunk = self._body[start : start + self._chunk_size]
            self._server.bytes_read += len(chunk)
            yield chunk


class _RecordingAsgiTransport(httpx.ASGITransport):
    def __init__(self, server: FakeMcpServer) -> None:
        super().__init__(app=server._asgi)
        self._server = server

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        server = self._server
        server.requests.append(request)
        if request.method == "POST" and server.fail_posts > 0:
            server.fail_posts -= 1
            return httpx.Response(500, json={"erro": "falha simulada"})
        key = json.loads(request.content).get("method", "") if request.method == "POST" else request.method
        if delay := server.delays.get(key, 0.0):
            await asyncio.sleep(delay)
        response = await super().handle_async_request(request)
        if server.chunk_size is None and not server.gzip:
            return response
        body = b"".join([chunk async for chunk in response.stream])  # type: ignore[union-attr]
        headers = httpx.Headers(response.headers)
        if server.gzip:
            body = gzip.compress(body)
            headers["Content-Encoding"] = "gzip"
        headers["Content-Length"] = str(len(body))
        stream = _CountedStream(body, server, server.chunk_size or max(len(body), 1))
        return httpx.Response(response.status_code, headers=headers, stream=stream, request=request)


class FakeMcpServer:
    """Servidor MCP falso; ver o docstring do módulo."""

    def __init__(self, tools: list[types.Tool] | None = None, *, stateless: bool = True) -> None:
        self.tools: list[types.Tool] = list(tools or [])
        self.results: dict[str, types.CallToolResult | str] = {}
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.requests: list[httpx.Request] = []
        self.fail_posts = 0
        self.delays: dict[str, float] = {}
        self.chunk_size: int | None = None
        self.bytes_read = 0
        self.gzip = False
        self._server: Server[Any, Any] = Server("fake-mcp")
        self._register_handlers()
        self._manager = StreamableHTTPSessionManager(app=self._server, json_response=True, stateless=stateless)
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    def _register_handlers(self) -> None:
        @self._server.list_tools()  # type: ignore[no-untyped-call,untyped-decorator]
        async def list_tools() -> list[types.Tool]:
            return list(self.tools)

        @self._server.call_tool(validate_input=False)  # type: ignore[untyped-decorator]
        async def call_tool(name: str, arguments: dict[str, Any]) -> types.CallToolResult:
            self.calls.append((name, arguments))
            result = self.results.get(name, f"ok {name}")
            if isinstance(result, str):
                return types.CallToolResult(content=[types.TextContent(type="text", text=result)])
            return result

    async def _asgi(self, scope: Any, receive: Any, send: Any) -> None:
        await self._manager.handle_request(scope, receive, send)

    def transport(self) -> httpx.AsyncBaseTransport:
        """Transporte httpx até o servidor (novo a cada chamada; todos gravam em ``requests``)."""
        return _RecordingAsgiTransport(self)

    def methods(self) -> list[str]:
        """Métodos JSON-RPC recebidos por POST, na ordem (``initialize``, ``tools/list``...)."""
        found: list[str] = []
        for request in self.requests:
            if request.method == "POST":
                body = json.loads(request.content)
                found.append(body.get("method", ""))
        return found

    async def __aenter__(self) -> FakeMcpServer:
        # O gerenciador do SDK (anyio) entra e sai na mesma task: uma task dona, como no cliente. Assim o
        # servidor também serve de fixture async (setup e teardown do pytest-asyncio rodam em tasks diferentes).
        ready = asyncio.get_running_loop().create_future()
        self._task = asyncio.create_task(self._serve(ready))
        await ready
        return self

    async def _serve(self, ready: asyncio.Future[None]) -> None:
        async with self._manager.run():
            ready.set_result(None)
            await self._stop.wait()

    async def __aexit__(self, *exc: object) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task
