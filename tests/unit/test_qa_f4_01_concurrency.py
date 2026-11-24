"""QA do F4-01: cancelamento, concorrência e shutdown do cliente HTTP compartilhado das tools.

O upstream é o backend de rede falso do httpcore: uma conexão que fica esperando a resposta (``hang``) simula
o servidor lento; ``aclose`` do stream a solta, como o fechamento do socket faria. Nada sai da máquina.
"""

from __future__ import annotations

import asyncio
import ssl
import typing

import httpcore
import httpx
import pytest

from src.domain.entities.tool import HttpMethod, ParameterType, Tool, ToolParameter
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from src.infrastructure.security.guarded_http import GuardedAsyncTransport
from src.infrastructure.security.ssrf import DestinationRule
from tests.fakes import RecordingLogger
from tests.fakes.dns import PUBLIC_IP
from tests.fakes.network_backend import OK_RESPONSE, RecordingAsyncBackend

_TOOL = Tool(
    id="t",
    name="T",
    description="d",
    route="https://api.example.com/itens",
    http_method=HttpMethod.GET,
    parameters=[ToolParameter("q", ParameterType.STRING, "q")],
)


class _HangingStream(httpcore.AsyncNetworkStream):
    def __init__(self, owner: _HangingBackend) -> None:
        self._owner = owner
        self._closed = asyncio.Event()

    async def read(self, max_bytes: int, timeout: float | None = None) -> bytes:  # noqa: ASYNC109
        self._owner.reading.set()
        await self._closed.wait()
        return b""  # soltada pelo fechamento: o servidor "caiu" sem responder

    async def write(self, buffer: bytes, timeout: float | None = None) -> None:  # noqa: ASYNC109
        return None

    async def aclose(self) -> None:
        self._owner.closed += 1
        self._closed.set()

    async def start_tls(
        self,
        ssl_context: ssl.SSLContext,
        server_hostname: str | None = None,
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> httpcore.AsyncNetworkStream:
        return self

    def get_extra_info(self, info: str) -> typing.Any:
        return None


class _HangingBackend(RecordingAsyncBackend):
    """Primeira ``hang`` conexões ficam penduradas; as seguintes respondem normalmente."""

    def __init__(self, hang: int = 1) -> None:
        super().__init__(OK_RESPONSE)
        self.hang = hang
        self.reading = asyncio.Event()
        self.closed = 0

    async def connect_tcp(self, host: str, port: int, *args: typing.Any, **kwargs: typing.Any) -> typing.Any:
        if len(self.connects) < self.hang:
            self._connect(host, port)
            return _HangingStream(self)
        return await super().connect_tcp(host, port, *args, **kwargs)


def _factory(backend: RecordingAsyncBackend, **kwargs: typing.Any) -> HttpToolFactory:
    transport = GuardedAsyncTransport(DestinationRule(), resolver=lambda host: [PUBLIC_IP], network_backend=backend)
    return HttpToolFactory(logger=RecordingLogger(), transport=transport, retry_backoff=0.0, **kwargs)


async def _function(factory: HttpToolFactory) -> typing.Callable[..., typing.Awaitable[str]]:
    (function,) = await factory.create_tools_from_configs([_TOOL])
    return function.entrypoint  # type: ignore[return-value]


async def test_cancelar_uma_chamada_em_voo_solta_a_conexao_e_nao_envenena_o_cliente():
    backend = _HangingBackend(hang=1)
    factory = _factory(backend)
    call = await _function(factory)

    task = asyncio.create_task(call(q="a"))
    await asyncio.wait_for(backend.reading.wait(), 2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert backend.closed == 1  # a conexão pendurada foi fechada, não ficou no pool
    assert await asyncio.wait_for(call(q="b"), 2) == "{'ok': 1}"
    await factory.aclose()


async def test_prazo_total_estourado_fecha_a_conexao_e_a_chamada_seguinte_funciona():
    backend = _HangingBackend(hang=3)
    factory = _factory(backend, timeout=0.05, retries=0)
    try:
        call = await _function(factory)

        assert await asyncio.wait_for(call(q="a"), 2) == "Erro na requisição: timeout ao chamar a tool (ReadTimeout)"
        assert backend.closed == 1
        assert await asyncio.wait_for(call(q="b"), 2) == "Erro na requisição: timeout ao chamar a tool (ReadTimeout)"
        assert backend.closed == 2
    finally:
        await factory.aclose()


async def test_shutdown_com_chamada_em_voo_termina_sem_travar_e_depois_nada_sai():
    backend = _HangingBackend(hang=1)
    factory = _factory(backend, retries=0)
    call = await _function(factory)

    in_flight = asyncio.create_task(call(q="a"))
    await asyncio.wait_for(backend.reading.wait(), 2)
    await asyncio.wait_for(factory.aclose(), 2)
    result = await asyncio.wait_for(in_flight, 2)
    connects_before = len(backend.connects)
    after = await call(q="b")

    assert result.startswith("Erro")
    assert after == "Erro inesperado ao chamar a tool (RuntimeError)"  # fechado: nenhuma conexão nova
    assert len(backend.connects) == connects_before


async def test_chamadas_paralelas_compartilham_o_cliente_sem_misturar_respostas():
    seen: list[httpx.Request] = []

    class Echo(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            seen.append(request)
            await asyncio.sleep(0)  # cede o loop: as chamadas se intercalam
            return httpx.Response(200, json={"q": request.url.params["q"]})

    factory = HttpToolFactory(logger=RecordingLogger(), transport=Echo(), retry_backoff=0.0)
    call = await _function(factory)

    results = await asyncio.gather(*(call(q=f"v{i}") for i in range(50)))

    assert results == [f"{{'q': 'v{i}'}}" for i in range(50)]
    assert len(seen) == 50
    await factory.aclose()


async def test_chamadas_paralelas_pelo_transporte_guardado_conectam_todas_no_ip_conferido():
    backend = RecordingAsyncBackend()
    factory = _factory(backend)
    call = await _function(factory)

    results = await asyncio.gather(*(call(q=f"v{i}") for i in range(20)))

    assert set(results) == {"{'ok': 1}"}
    assert {connect for connect in backend.connects} == {(PUBLIC_IP, 443)}
    assert len(backend.connects) == 20  # Connection: close: nenhuma reaproveitada
    await factory.aclose()
