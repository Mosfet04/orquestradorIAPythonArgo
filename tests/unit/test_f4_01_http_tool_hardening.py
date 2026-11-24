"""F4-01: tool HTTP endurecida. Cliente compartilhado, limites, retry com jitter, guarda na conexão e rota.

Upstream falso: ``httpx.MockTransport`` injetado no ``transport=`` da fábrica (ou o transporte guardado sobre
o backend de rede falso). Nada sai da máquina.
"""

from __future__ import annotations

import asyncio
import gzip
import json
import time
import tracemalloc
import zlib
from collections.abc import Callable
from typing import Any

import httpx
import pytest

from src.domain.entities.tool import HttpMethod, ParameterType, Tool, ToolParameter
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory, backoff_delay
from src.infrastructure.security.guarded_http import GuardedAsyncTransport
from src.infrastructure.security.ssrf import DestinationRule
from tests.fakes import RecordingLogger
from tests.fakes.network_backend import RecordingAsyncBackend


def _tool(route: str = "https://api.example.com/itens", method: HttpMethod = HttpMethod.GET, **kw: Any) -> Tool:
    params = kw.pop("parameters", [ToolParameter(name="q", type=ParameterType.STRING, description="q")])
    return Tool(id="t", name="T", description="d", route=route, http_method=method, parameters=params, **kw)


class _Upstream(httpx.AsyncBaseTransport):
    """``MockTransport`` que conta requests e fechamentos; ``responses`` é consumida em ordem (a última se repete)."""

    def __init__(self, *responses: Callable[[httpx.Request], httpx.Response]) -> None:
        self.responses = list(responses) or [lambda r: httpx.Response(200, json={"ok": 1})]
        self.seen: list[httpx.Request] = []
        self.closed = 0

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(request)
        handler = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        return handler(request)

    async def aclose(self) -> None:
        self.closed += 1


def _raise(exc: type[httpx.TransportError]) -> Callable[[httpx.Request], httpx.Response]:
    def handler(request: httpx.Request) -> httpx.Response:
        raise exc("falha com texto interno: Authorization: Bearer sk-x", request=request)

    return handler


async def _call(factory: HttpToolFactory, tool: Tool, **arguments: Any) -> str:
    (function,) = await factory.create_tools_from_configs([tool])
    result: str = await function.entrypoint(**arguments)
    return result


def _factory(transport: httpx.AsyncBaseTransport, logger: RecordingLogger | None = None, **kw: Any) -> HttpToolFactory:
    return HttpToolFactory(logger=logger or RecordingLogger(), transport=transport, retry_backoff=0.0, **kw)


# ── cliente compartilhado ───────────────────────────────────────────


async def test_um_cliente_para_todas_as_chamadas_fechado_no_aclose():
    upstream = _Upstream()
    factory = _factory(upstream)
    tool = _tool()
    assert await _call(factory, tool, q="a") == "{'ok': 1}"
    assert await _call(factory, tool, q="b") == "{'ok': 1}"
    assert upstream.closed == 0  # nada de cliente por chamada
    await factory.aclose()
    assert upstream.closed == 1
    await factory.aclose()  # idempotente
    assert upstream.closed == 1
    assert await _call(factory, tool, q="c") == "Erro inesperado ao chamar a tool (RuntimeError)"  # não reabre
    assert len(upstream.seen) == 2


async def test_redirect_nao_e_seguido_e_volta_como_erro():
    upstream = _Upstream(lambda r: httpx.Response(302, headers={"Location": "http://169.254.169.254/"}))
    result = await _call(_factory(upstream), _tool(), q="x")
    assert result.startswith("Erro HTTP 302")
    assert len(upstream.seen) == 1


# ── retry com jitter ────────────────────────────────────────────────


@pytest.mark.parametrize("method", [HttpMethod.GET, HttpMethod.PUT, HttpMethod.DELETE])
async def test_metodo_idempotente_repete_em_503_e_devolve_o_sucesso(method: HttpMethod):
    upstream = _Upstream(lambda r: httpx.Response(503), lambda r: httpx.Response(200, json={"v": 2}))
    assert await _call(_factory(upstream), _tool(method=method), q="x") == "{'v': 2}"
    assert len(upstream.seen) == 2


@pytest.mark.parametrize("method", [HttpMethod.POST, HttpMethod.PATCH])
async def test_metodo_nao_idempotente_nao_repete_em_503(method: HttpMethod):
    upstream = _Upstream(lambda r: httpx.Response(503, text="fora"))
    assert await _call(_factory(upstream), _tool(method=method), q="x") == "Erro HTTP 503: fora"
    assert len(upstream.seen) == 1


async def test_status_de_erro_do_cliente_nao_repete():
    upstream = _Upstream(lambda r: httpx.Response(404, text="nao achou"))
    assert await _call(_factory(upstream), _tool(), q="x") == "Erro HTTP 404: nao achou"
    assert len(upstream.seen) == 1


async def test_falha_de_rede_transiente_repete_ate_o_limite_e_devolve_erro_sanitizado():
    logger = RecordingLogger()
    upstream = _Upstream(_raise(httpx.ReadTimeout))
    result = await _call(_factory(upstream, logger, retries=2), _tool(), q="x")
    assert result == "Erro na requisição: timeout ao chamar a tool (ReadTimeout)"
    assert len(upstream.seen) == 3
    assert "sk-x" not in repr(logger.records)


async def test_post_repete_so_quando_a_conexao_nem_abriu():
    connect = _Upstream(_raise(httpx.ConnectError), lambda r: httpx.Response(201, json={"id": 1}))
    assert await _call(_factory(connect), _tool(method=HttpMethod.POST), q="x") == "{'id': 1}"
    assert len(connect.seen) == 2
    read = _Upstream(_raise(httpx.ReadTimeout))
    result = await _call(_factory(read), _tool(method=HttpMethod.POST), q="x")
    assert result == "Erro na requisição: timeout ao chamar a tool (ReadTimeout)"
    assert len(read.seen) == 1  # o corpo pode ter chegado: repetir duplicaria o efeito


def test_backoff_com_jitter_fica_entre_zero_e_o_teto_exponencial():
    seen: list[tuple[float, float]] = []

    def uniform(low: float, high: float) -> float:
        seen.append((low, high))
        return high

    assert [backoff_delay(attempt, 0.25, uniform) for attempt in range(4)] == [0.25, 0.5, 1.0, 2.0]
    assert seen == [(0.0, 0.25), (0.0, 0.5), (0.0, 1.0), (0.0, 2.0)]
    assert backoff_delay(10, 0.25, uniform) == 5.0  # teto


# ── limite de resposta ──────────────────────────────────────────────


async def test_resposta_acima_do_limite_e_cortada_sem_chegar_ao_modelo():
    async def chunks():
        yield b"x" * 1024
        yield b"x" * 1024  # sem Content-Length: o corte é na leitura

    upstream = _Upstream(lambda r: httpx.Response(200, content=chunks()))
    result = await _call(_factory(upstream, max_response_bytes=1500), _tool(), q="x")
    assert result == "Erro na tool t: resposta acima do limite de 1500 bytes"


async def test_content_length_acima_do_limite_recusa_antes_de_ler():
    upstream = _Upstream(lambda r: httpx.Response(200, headers={"Content-Length": "999999"}, content=b"{}"))
    result = await _call(_factory(upstream, max_response_bytes=1000), _tool(), q="x")
    assert result == "Erro na tool t: resposta acima do limite de 1000 bytes"


async def test_corpo_de_erro_tambem_respeita_o_limite():
    upstream = _Upstream(lambda r: httpx.Response(500, content=b"y" * 5000))
    result = await _call(_factory(upstream, max_response_bytes=100), _tool(), q="x")
    assert result == "Erro na tool t: resposta acima do limite de 100 bytes"


async def test_resposta_dentro_do_limite_respeita_o_charset():
    latin = {"Content-Type": "text/plain; charset=latin-1"}
    upstream = _Upstream(lambda r: httpx.Response(200, headers=latin, content="ação".encode("latin-1")))
    assert await _call(_factory(upstream), _tool(), q="x") == "ação"


# ── guarda na conexão ───────────────────────────────────────────────


async def test_destino_recusado_pela_guarda_nao_repete_e_nao_cita_o_host():
    logger = RecordingLogger()
    calls: list[str] = []

    def private(host: str) -> list[str]:
        calls.append(host)
        return ["10.0.0.5"]

    backend = RecordingAsyncBackend()
    transport = GuardedAsyncTransport(DestinationRule(), resolver=private, network_backend=backend)
    result = await _call(_factory(transport, logger), _tool(route="https://interno-secreto.example.com/x"), q="a")
    assert result == "Erro na tool t: destino recusado pela guarda de rede"
    assert calls == ["interno-secreto.example.com"]
    assert backend.connects == []
    assert "interno-secreto" not in repr([r.context for r in logger.records])
    assert any(r.context.get("error_type") == "BlockedDestinationError" for r in logger.records)


async def test_destino_conferido_conecta_no_ip_resolvido():
    backend = RecordingAsyncBackend()
    transport = GuardedAsyncTransport(DestinationRule(), resolver=lambda h: ["93.184.216.34"], network_backend=backend)
    assert await _call(_factory(transport), _tool(), q="a") == "{'ok': 1}"
    assert backend.connects == [("93.184.216.34", 443)]


# ── rota ────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "route", ["https://{host}/x", "https://api.{dom}.com/x", "https://api.example.com:{porta}/x", "{esquema}://a.com/"]
)
async def test_placeholder_no_esquema_ou_no_host_recusa_a_tool_na_criacao(route: str):
    name = route.split("{", 1)[1].split("}", 1)[0]
    logger = RecordingLogger()
    param = ToolParameter(name=name, type=ParameterType.STRING, description="h", required=True)
    tool = _tool(route=route, parameters=[param])
    assert await _factory(_Upstream(), logger).create_tools_from_configs([tool]) == []
    assert any("placeholder" in r.message for r in logger.records if r.level == "error")


async def test_parametro_de_rota_vazio_e_recusado_sem_chamar_o_upstream():
    upstream = _Upstream()
    tool = _tool(
        route="https://api.example.com/clientes/{id}/pedidos",
        parameters=[ToolParameter(name="id", type=ParameterType.STRING, description="id", required=True)],
    )
    result = await _call(_factory(upstream), tool, id="")
    assert result.startswith("Erro nos argumentos da tool t: valor inválido para id")
    assert upstream.seen == []


async def test_query_fixa_da_rota_e_mantida_junto_dos_argumentos():
    upstream = _Upstream()
    await _call(_factory(upstream), _tool(route="https://api.example.com/busca?versao=2&fmt=json"), q="a b")
    assert str(upstream.seen[0].url) == "https://api.example.com/busca?versao=2&fmt=json&q=a+b"


async def test_query_fixa_sem_argumentos_fica_como_esta():
    upstream = _Upstream()
    await _call(_factory(upstream), _tool(route="https://api.example.com/busca?versao=2"))
    assert str(upstream.seen[0].url) == "https://api.example.com/busca?versao=2"


# ── revisão da F4-01: cookie, compressão, JSON profundo, prazo total ──


async def test_cookie_de_uma_tool_nao_vai_na_chamada_de_outra():
    """O cliente é compartilhado: um ``Set-Cookie`` guardado vazaria a sessão de uma tool/usuário para outra."""

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/login":
            return httpx.Response(200, json={"ok": 1}, headers={"Set-Cookie": "session=user-A-secret; Path=/"})
        return httpx.Response(200, json={"ok": 2})

    upstream = _Upstream(handler)
    factory = _factory(upstream)
    login = _tool(route="https://api.example.com/login", method=HttpMethod.POST, parameters=[])
    conta = Tool(
        id="b", name="B", description="d", route="https://api.example.com/conta", http_method=HttpMethod.GET,
        parameters=[],
    )
    first, second = await factory.create_tools_from_configs([login, conta])
    assert await first.entrypoint() == "{'ok': 1}"
    assert await second.entrypoint() == "{'ok': 2}"
    assert await second.entrypoint() == "{'ok': 2}"
    assert [request.headers.get("cookie") for request in upstream.seen] == [None, None, None]


def _streamed(
    compressed: bytes, encoding: str = "gzip", chunk: int = 64 * 1024
) -> Callable[[httpx.Request], httpx.Response]:
    """Resposta com o corpo em pedaços (como chega do socket), não já lido pelo ``MockTransport``."""

    async def chunks():
        for start in range(0, len(compressed), chunk):
            yield compressed[start : start + chunk]

    return lambda r: httpx.Response(200, headers={"Content-Encoding": encoding}, content=chunks())


async def test_gzip_bomba_e_cortada_sem_descomprimir_alem_do_limite():
    upstream = _Upstream(_streamed(gzip.compress(b"0" * 20_000_000)))  # ~20 KB comprimidos, 20 MB abertos
    tracemalloc.start()
    try:
        result = await _call(_factory(upstream, max_response_bytes=1024 * 1024), _tool(), q="x")
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
    assert result == f"Erro na tool t: resposta acima do limite de {1024 * 1024} bytes"
    assert peak < 6 * 1024 * 1024


@pytest.mark.parametrize("encoding, compress", [("gzip", gzip.compress), ("deflate", zlib.compress)])
async def test_corpo_comprimido_dentro_do_limite_e_descomprimido(encoding: str, compress: Callable[[bytes], bytes]):
    items = [{"id": i} for i in range(2000)]  # ~22 KB abertos, em pedaços de 7 bytes comprimidos
    upstream = _Upstream(_streamed(compress(json.dumps(items).encode()), encoding, chunk=7))
    assert await _call(_factory(upstream), _tool(), q="x") == str(items)
    assert upstream.seen[0].headers["Accept-Encoding"] == "gzip, deflate"


async def test_codificacao_sem_suporte_e_recusada_com_erro_curto():
    upstream = _Upstream(lambda r: httpx.Response(200, headers={"Content-Encoding": "br"}, content=b"\x8b\x00"))
    result = await _call(_factory(upstream), _tool(), q="x")
    assert result == "Erro na tool t: Content-Encoding da resposta sem suporte"


async def test_gzip_corrompido_vira_erro_de_decodificacao_sem_texto():
    upstream = _Upstream(_streamed(b"nao-e-gzip"))
    result = await _call(_factory(upstream), _tool(), q="x")
    assert result == "Erro na requisição: falha ao chamar a tool (DecodingError)"


async def test_json_profundo_demais_volta_como_texto():
    body = b"[" * 200_000 + b"]" * 200_000
    upstream = _Upstream(lambda r: httpx.Response(200, headers={"Content-Type": "application/json"}, content=body))
    assert await _call(_factory(upstream), _tool(), q="x") == body.decode()


async def test_upstream_gotejando_estoura_o_prazo_total_da_tentativa():
    """O timeout do httpx é por operação: 1 byte a cada intervalo menor que ele seguraria a tool sem fim."""

    async def drip():
        for _ in range(200):
            yield b"x"
            await asyncio.sleep(0.02)

    upstream = _Upstream(lambda r: httpx.Response(200, content=drip()))
    started = time.monotonic()
    result = await _call(_factory(upstream, timeout=0.2, retries=0), _tool(), q="x")
    assert result == "Erro na requisição: timeout ao chamar a tool (ReadTimeout)"
    assert time.monotonic() - started < 2.0


async def test_corpo_ja_lido_pelo_transporte_tambem_respeita_o_limite():
    """``MockTransport`` devolve o corpo já aberto: o ``Content-Length`` é o comprimido, o limite vale no aberto."""
    body = gzip.compress(b"0" * 5000)
    upstream = _Upstream(lambda r: httpx.Response(200, headers={"Content-Encoding": "gzip"}, content=body))
    result = await _call(_factory(upstream, max_response_bytes=1000), _tool(), q="x")
    assert result == "Erro na tool t: resposta acima do limite de 1000 bytes"
