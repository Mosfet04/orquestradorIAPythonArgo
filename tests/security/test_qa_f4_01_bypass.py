"""QA do F4-01: tentativas de bypass da guarda SSRF da tool HTTP, pela pilha inteira.

Pilha real: ``HttpToolFactory`` -> ``httpx.AsyncClient`` -> ``GuardedAsyncTransport`` -> pool do httpcore ->
backend de rede falso (``tests/fakes/network_backend.py``: grava o connect pedido e os bytes que iriam ao
fio). Nada resolve DNS real nem abre socket. Cobre: formas de IP (legadas, IPv6, mapeadas, NAT64, 6to4),
confusão de userinfo/barra invertida, nome que resolve para lixo ou para rede interna, rebinding entre
chamadas, redirect, vazamento de segredo de header em erro/log, retry que duplicaria efeito (nos bytes do
fio), injeção por argumento do modelo, ``Content-Encoding``/limites no fio e envenenamento do pool.
"""

from __future__ import annotations

import asyncio
import gzip
import socket
import zlib
from collections.abc import Callable
from typing import Any

import httpcore
import pytest

from src.domain.entities.tool import HttpMethod, ParameterType, Tool, ToolParameter
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from src.infrastructure.security.guarded_http import GuardedAsyncTransport
from src.infrastructure.security.ssrf import DestinationRule
from tests.fakes import RecordingLogger
from tests.fakes.dns import PUBLIC_IP
from tests.fakes.network_backend import RecordingAsyncBackend, http_response

SECRET = "sk-qa-f401-valor-que-nunca-sai"  # noqa: S105 - marcador de vazamento, não é segredo
REFUSED = "Erro na tool t: destino recusado pela guarda de rede"


class _Dns:
    """Resolver falso: ``answers`` por chamada (a última se repete); exceção se for uma ``BaseException``."""

    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)
        self.calls: list[str] = []

    def __call__(self, host: str) -> list[str]:
        self.calls.append(host)
        answer = self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return list(answer)


def _tool(route: str = "https://api.example.com/itens", method: HttpMethod = HttpMethod.GET, **kw: Any) -> Tool:
    parameters = kw.pop("parameters", [])
    return Tool(id="t", name="T", description="d", route=route, http_method=method, parameters=parameters, **kw)


class _Stack:
    def __init__(
        self,
        dns: Any = None,
        backend: RecordingAsyncBackend | None = None,
        rule: DestinationRule | None = None,
        **factory_kwargs: Any,
    ) -> None:
        self.dns = dns or _Dns([PUBLIC_IP])
        self.backend = backend or RecordingAsyncBackend()
        self.logger = RecordingLogger()
        transport = GuardedAsyncTransport(rule or DestinationRule(), resolver=self.dns, network_backend=self.backend)
        self.factory = HttpToolFactory(
            logger=self.logger, transport=transport, retry_backoff=0.0, **factory_kwargs
        )

    async def call(self, tool: Tool, **arguments: Any) -> str:
        (function,) = await self.factory.create_tools_from_configs([tool])
        result: str = await function.entrypoint(**arguments)
        return result

    def logged(self) -> str:
        return repr(self.logger.records)


# ── formas de IP e confusão de URL ──────────────────────────────────

_HOSTILE_ROUTES = [
    "http://2130706433/x",  # 127.0.0.1 em decimal
    "http://0x7f.0.0.1/x",
    "http://127.1/x",
    "http://[::1]/x",
    "http://[::ffff:127.0.0.1]/x",
    "http://[::ffff:a9fe:a9fe]/x",  # 169.254.169.254 mapeado, em hexa
    "http://[::ffff:169.254.169.254]/x",
    "http://169.254.169.254/x",
    "http://0xa9fea9fe/x",
    "http://169.254.43518/x",
    "http://[fd00:ec2::254]/x",  # metadata AWS em IPv6
    "http://0.0.0.0/x",
    "http://[::]/x",
    "http://10.0.0.1/x",
    "http://192.168.1.1:8080/x",
    "http://100.64.0.1/x",  # CGNAT
    "http://198.18.0.1/x",  # benchmark
    "http://224.0.0.1/x",  # multicast
    "http://[ff02::1]/x",
    "http://[fec0::1]/x",  # site-local IPv6 (o is_global do 3.12 diz global)
    "http://[64:ff9b::a9fe:a9fe]/x",  # NAT64 -> metadata
    "http://[2002:a9fe:a9fe::]/x",  # 6to4 -> metadata
    "http://[fe80::1%25eth0]/x",  # link-local com escopo
    "http://allowed.example.com@169.254.169.254/x",  # userinfo: o host real é o do metadata
    "http://allowed.example.com\\@169.254.169.254/x",  # barra invertida
    "http://0177.0.0.1/x",  # octal: o httpx recusa a URL; em nenhum caso conecta
    "http://127.0.0.1\t/x",
    "file:///etc/passwd",  # esquema fora de http(s): documento legado com route fora do contrato
    "ftp://example.com/x",
    "gopher://example.com/",
    "unix:///var/run/docker.sock",
    "//example.com/x",
    "example.com/x",
    "http:///x",
    "http://:80/x",
]


@pytest.mark.parametrize("route", _HOSTILE_ROUTES)
async def test_rota_hostil_nunca_abre_conexao_nem_consulta_dns(route: str):
    stack = _Stack(dns=_Dns(AssertionError("literal não resolve DNS")))
    result = await stack.call(_tool(route=route))

    assert result.startswith("Erro")
    assert stack.backend.connects == []
    assert stack.backend.sent() == b""
    assert stack.dns.calls == []


@pytest.mark.parametrize("route", ["http://169.254.169.254./x", "http://metadata.google.internal/x", "http://localhost/x"])
async def test_nome_que_resolve_para_rede_interna_e_recusado_sem_conectar(route: str):
    stack = _Stack(dns=_Dns(["169.254.169.254" if "169" in route else "10.0.0.9"]))
    assert await stack.call(_tool(route=route)) == REFUSED
    assert stack.backend.connects == []


async def test_userinfo_com_ip_proibido_nao_muda_o_destino_real():
    """``http://169.254.169.254:80@allowed.example.com/``: o host é ``allowed.example.com`` (o IP é usuário)."""
    stack = _Stack(dns=_Dns([PUBLIC_IP]))
    result = await stack.call(_tool(route="http://169.254.169.254:80@allowed.example.com/x"))

    assert result == "{'ok': 1}"
    assert stack.backend.connects == [(PUBLIC_IP, 80)]
    assert stack.dns.calls == ["allowed.example.com"]
    assert b"Host: allowed.example.com\r\n" in stack.backend.sent()


@pytest.mark.parametrize(
    "answer",
    [
        [PUBLIC_IP, "10.0.0.5"],  # um público e um privado: um só proibido recusa tudo
        ["::ffff:10.0.0.5"],
        ["::ffff:169.254.169.254"],
        ["fe80::1%eth0"],
        ["fec0::1"],
        ["64:ff9b::a9fe:a9fe"],
        ["0x7f.1"],  # forma legada de 127.0.0.1 devolvida pelo resolver
        ["2130706433"],
        ["not-an-ip"],
        [""],
        [],
        [PUBLIC_IP, "not-an-ip"],
        socket.gaierror(-2, "Name or service not known"),
        UnicodeError("label too long"),
    ],
    ids=repr,
)
async def test_resposta_hostil_do_resolver_recusa_sem_conectar(answer: Any):
    stack = _Stack(dns=_Dns(answer))
    assert await stack.call(_tool(route="https://api.example.com/x")) == REFUSED
    assert stack.backend.connects == []
    assert stack.backend.sent() == b""


async def test_rebinding_entre_chamadas_recusa_a_segunda_e_o_pool_segue_saudavel():
    stack = _Stack(dns=_Dns([PUBLIC_IP], ["169.254.169.254"], ["10.0.0.5"], [PUBLIC_IP]))
    tool = _tool()
    results = [await stack.call(tool) for _ in range(4)]

    assert results == ["{'ok': 1}", REFUSED, REFUSED, "{'ok': 1}"]
    assert stack.backend.connects == [(PUBLIC_IP, 443), (PUBLIC_IP, 443)]  # as recusadas nem chegaram ao TCP


async def test_pool_nao_fica_envenenado_apos_connect_recusado_pelo_destino():
    backend = RecordingAsyncBackend(refuse=("8.8.8.8",))
    stack = _Stack(dns=_Dns(["8.8.8.8"], [PUBLIC_IP]), backend=backend, retries=0)
    first = await stack.call(_tool())
    second = await stack.call(_tool())

    assert first.startswith("Erro na requisição: falha ao chamar a tool (ConnectError)")
    assert second == "{'ok': 1}"


# ── host confiável e loopback (e2e) ─────────────────────────────────


async def test_host_do_operador_pode_ser_privado_mas_so_ele():
    rule = DestinationRule(trusted_hosts=frozenset({"api.interna.example"}))
    trusted = _Stack(dns=_Dns(["10.0.0.5"]), rule=rule)
    other = _Stack(dns=_Dns(["10.0.0.5"]), rule=rule)

    assert await trusted.call(_tool(route="https://api.interna.example/x")) == "{'ok': 1}"
    assert trusted.backend.connects == [("10.0.0.5", 443)]
    assert await other.call(_tool(route="https://outro.example/x")) == REFUSED  # mesmo IP, host não listado
    assert other.backend.connects == []


async def test_host_do_operador_que_passa_a_resolver_para_metadata_e_recusado():
    rule = DestinationRule(trusted_hosts=frozenset({"api.interna.example"}))
    stack = _Stack(dns=_Dns(["169.254.169.254"]), rule=rule)
    assert await stack.call(_tool(route="https://api.interna.example/x")) == REFUSED
    assert stack.backend.connects == []


@pytest.mark.parametrize(
    ("route", "answer", "connects"),
    [
        ("http://localhost/x", ["127.0.0.1"], [("127.0.0.1", 80)]),
        ("http://127.0.0.1:8080/x", None, [("127.0.0.1", 8080)]),
        ("http://[::1]/x", None, [("::1", 80)]),
        ("http://localhost./x", ["127.0.0.1"], []),  # N10: nome DNS, não loopback literal
        ("http://localhost/x", ["127.0.0.1", "10.0.0.1"], []),  # localhost que resolve fora do loopback
        ("http://2130706433/x", None, []),  # forma legada não conta como loopback literal
        ("http://[::ffff:127.0.0.1]/x", None, []),
    ],
)
async def test_modo_dev_local_libera_so_o_loopback_literal(
    route: str, answer: list[str] | None, connects: list[tuple[str, int]]
):
    dns = _Dns(answer) if answer is not None else _Dns(AssertionError("literal não resolve"))
    stack = _Stack(dns=dns, rule=DestinationRule(allow_loopback=True))
    result = await stack.call(_tool(route=route))

    assert stack.backend.connects == connects
    assert (result == "{'ok': 1}") is bool(connects)


# ── redirect ────────────────────────────────────────────────────────


@pytest.mark.parametrize("status", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("method", [HttpMethod.GET, HttpMethod.POST])
async def test_redirect_para_metadata_nao_e_seguido_e_o_segredo_nao_vai_junto(
    monkeypatch: pytest.MonkeyPatch, status: int, method: HttpMethod
):
    monkeypatch.setenv("QAF401_API_KEY", SECRET)
    backend = RecordingAsyncBackend(
        http_response(status, b"", {"Location": "http://169.254.169.254/latest/meta-data"})
    )
    stack = _Stack(backend=backend)
    tool = _tool(method=method, headers={"Authorization": "env:QAF401_API_KEY"})
    result = await stack.call(tool)

    assert result.startswith(f"Erro HTTP {status}")
    assert backend.connects == [(PUBLIC_IP, 443)]  # uma conexão: a do destino da rota
    assert backend.sent().count(b"HTTP/1.1\r\n") == 1  # uma request no fio
    assert b"169.254.169.254" not in backend.sent()


# ── segredo de header em erro, log e resultado ──────────────────────


def _failing(exc: Exception) -> RecordingAsyncBackend:
    class Failing(RecordingAsyncBackend):
        async def connect_tcp(self, host: str, port: int, *args: Any, **kwargs: Any) -> Any:
            self._connect(host, port)
            raise exc

    return Failing()


@pytest.mark.parametrize(
    "scenario", ["destino_recusado", "connect_recusado", "connect_com_texto_do_segredo", "http_500"]
)
async def test_segredo_de_header_nao_aparece_em_resultado_nem_em_log(monkeypatch: pytest.MonkeyPatch, scenario: str):
    monkeypatch.setenv("QAF401_API_KEY", SECRET)
    leaking = httpcore.ConnectError(f"Authorization: Bearer {SECRET}")
    stack = {
        "destino_recusado": _Stack(dns=_Dns(["10.0.0.5"])),
        "connect_recusado": _Stack(backend=RecordingAsyncBackend(refuse=(PUBLIC_IP,))),
        "connect_com_texto_do_segredo": _Stack(backend=_failing(leaking)),
        "http_500": _Stack(backend=RecordingAsyncBackend(http_response(500, b"erro do upstream"))),
    }[scenario]
    result = await stack.call(_tool(headers={"Authorization": "env:QAF401_API_KEY"}))

    assert result.startswith("Erro")
    assert SECRET not in result
    assert SECRET not in stack.logged()
    if scenario == "destino_recusado":
        assert stack.backend.sent() == b""  # o segredo nem chegou ao fio do destino recusado


async def test_referencia_de_header_que_nao_resolve_nao_chama_o_upstream_nem_cita_o_valor(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.delenv("QAF401_API_KEY", raising=False)
    stack = _Stack()
    result = await stack.call(_tool(headers={"Authorization": "env:QAF401_API_KEY"}))

    assert result == "Erro na tool t: header por referência não resolvido (Authorization)"
    assert stack.backend.connects == []
    assert "QAF401" not in stack.logged()


# ── retry duplicando efeito (nos bytes do fio) ──────────────────────

_LOST_RESPONSE = b""  # o servidor aceita a conexão, recebe a request e fecha sem responder


@pytest.mark.parametrize(
    ("method", "attempts"),
    [
        (HttpMethod.GET, 3),
        (HttpMethod.PUT, 3),
        (HttpMethod.DELETE, 3),
        (HttpMethod.POST, 1),  # a request pode ter sido processada: repetir duplicaria o efeito
        (HttpMethod.PATCH, 1),
    ],
)
async def test_resposta_perdida_so_repete_metodo_idempotente(method: HttpMethod, attempts: int):
    backend = RecordingAsyncBackend(_LOST_RESPONSE)
    stack = _Stack(backend=backend, retries=2)
    tool = _tool(method=method, parameters=[ToolParameter("n", ParameterType.INTEGER, "n", required=True)])
    result = await stack.call(tool, n=7)

    assert "RemoteProtocolError" in result
    assert len(backend.connects) == attempts
    assert backend.sent().count(f"{method.value} /itens".encode()) == attempts


@pytest.mark.parametrize("status", [502, 503, 504])
async def test_post_com_gateway_error_nao_repete(status: int):
    backend = RecordingAsyncBackend(http_response(status, b"gateway"))
    stack = _Stack(backend=backend, retries=2)
    assert await stack.call(_tool(method=HttpMethod.POST)) == f"Erro HTTP {status}: gateway"
    assert len(backend.connects) == 1


async def test_get_com_503_persistente_para_no_limite_de_tentativas():
    backend = RecordingAsyncBackend(http_response(503, b"fora"))
    stack = _Stack(backend=backend, retries=3)
    assert await stack.call(_tool()) == "Erro HTTP 503: fora"
    assert len(backend.connects) == 4


# ── injeção por argumento do modelo ─────────────────────────────────


@pytest.mark.parametrize(
    "value",
    [
        "x\r\nX-Injected: 1",
        "../../admin",
        "..%2f..%2fadmin",
        "@169.254.169.254",
        "evil.example#",
        "evil.example?x=1",
        "a/b",
        "\x00",
    ],
)
async def test_argumento_de_rota_e_de_query_nao_injeta_header_caminho_nem_destino(value: str):
    stack = _Stack()
    tool = _tool(
        route="https://api.example.com/itens/{id}?fixo=1",
        parameters=[
            ToolParameter("id", ParameterType.STRING, "id", required=True),
            ToolParameter("q", ParameterType.STRING, "q"),
        ],
    )
    result = await stack.call(tool, id=value, q=value)

    wire = stack.backend.sent()
    request_line = wire.split(b"\r\n", 1)[0]
    assert result == "{'ok': 1}"
    assert stack.backend.connects == [(PUBLIC_IP, 443)]
    assert request_line.startswith(b"GET /itens/") and request_line.endswith(b" HTTP/1.1")
    assert b"\r\nX-Injected" not in wire
    assert b"/itens/" in request_line and b"?fixo=1&q=" in request_line
    path = request_line.split(b" ")[1].split(b"?")[0]
    assert path.count(b"/") == 2  # "/itens/<um segmento>": o valor não criou nem tirou segmento
    assert b"Host: api.example.com\r\n" in wire  # o valor não trocou o host


async def test_corpo_json_do_post_escapa_o_que_o_modelo_manda():
    stack = _Stack()
    tool = _tool(method=HttpMethod.POST, parameters=[ToolParameter("msg", ParameterType.STRING, "m", required=True)])
    await stack.call(tool, msg='x"}\r\nX-Injected: 1')

    head, _, body = stack.backend.sent().partition(b"\r\n\r\n")
    assert b"\r\nX-Injected" not in head
    assert body == b'{"msg":"x\\"}\\r\\nX-Injected: 1"}'


# ── Content-Encoding e limites, no fio ──────────────────────────────


def _deflate(data: bytes) -> bytes:
    return zlib.compress(data)


@pytest.mark.parametrize("encoding", ["br", "zstd", "compress", "gzip, gzip", "gzip,identity", "deflate, br", "GZIP2"])
async def test_content_encoding_fora_de_gzip_deflate_identity_e_recusado(encoding: str):
    body = gzip.compress(b'{"a":1}')
    stack = _Stack(backend=RecordingAsyncBackend(http_response(200, body, {"Content-Encoding": encoding})))
    assert await stack.call(_tool()) == "Erro na tool t: Content-Encoding da resposta sem suporte"


@pytest.mark.parametrize(
    ("encoding", "compress"),
    [
        ("GZIP", gzip.compress),
        (" gzip ", gzip.compress),
        ("x-gzip", gzip.compress),
        ("deflate", _deflate),
        ("identity", lambda data: data),
    ],
)
async def test_content_encoding_aceito_e_descomprimido_no_fio(encoding: str, compress: Callable[[bytes], bytes]):
    body = compress(b'{"a": 1}')
    stack = _Stack(backend=RecordingAsyncBackend(http_response(200, body, {"Content-Encoding": encoding})))
    assert await stack.call(_tool()) == "{'a': 1}"


async def test_limite_do_corpo_e_exato_no_fio():
    limit = 1024 * 1024  # o padrão
    exact = b'"' + b"a" * (limit - 2) + b'"'
    over = b'"' + b"a" * (limit - 1) + b'"'
    assert len(exact) == limit and len(over) == limit + 1
    ok = _Stack(backend=RecordingAsyncBackend(http_response(200, exact)))
    too_big = _Stack(backend=RecordingAsyncBackend(http_response(200, over)))

    assert len(await ok.call(_tool())) == limit - 2  # JSON string decodificada
    assert await too_big.call(_tool()) == f"Erro na tool t: resposta acima do limite de {limit} bytes"


async def test_limite_vale_para_o_descomprimido_no_limite_exato_e_um_a_mais():
    limit = 64
    exact = b'"' + b"a" * (limit - 2) + b'"'
    over = exact + b" "
    ok = _Stack(backend=RecordingAsyncBackend(http_response(200, gzip.compress(exact), {"Content-Encoding": "gzip"})),
                max_response_bytes=limit)
    big = _Stack(backend=RecordingAsyncBackend(http_response(200, gzip.compress(over), {"Content-Encoding": "gzip"})),
                 max_response_bytes=limit)

    assert await ok.call(_tool()) == "a" * (limit - 2)
    assert await big.call(_tool()) == f"Erro na tool t: resposta acima do limite de {limit} bytes"


def _chunked(total: int, piece: int = 100) -> bytes:
    head = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\nConnection: close\r\n\r\n"
    sizes = [min(piece, total - i) for i in range(0, total, piece)]
    chunks = b"".join(f"{size:x}\r\n".encode() + b"a" * size + b"\r\n" for size in sizes)
    return head + chunks + b"0\r\n\r\n"


async def test_corpo_chunked_sem_content_length_acima_do_limite_e_cortado():
    stack = _Stack(backend=RecordingAsyncBackend(_chunked(5000)), max_response_bytes=1000)
    assert await stack.call(_tool()) == "Erro na tool t: resposta acima do limite de 1000 bytes"


async def test_corpo_chunked_dentro_do_limite_passa():
    stack = _Stack(backend=RecordingAsyncBackend(_chunked(300)), max_response_bytes=1000)
    assert await stack.call(_tool()) == "a" * 300


async def test_gzip_com_lixo_depois_do_membro_nao_estoura_nem_vaza_texto():
    body = gzip.compress(b'{"a": 1}') + b"\x00lixo" * 10
    stack = _Stack(backend=RecordingAsyncBackend(http_response(200, body, {"Content-Encoding": "gzip"})))
    result = await stack.call(_tool())

    assert result == "{'a': 1}"


async def test_gzip_truncado_ou_corrompido_vira_erro_curto_sem_o_texto_da_biblioteca():
    corrupt = b"\x1f\x8b\x08\x00" + b"\xff" * 30
    stack = _Stack(backend=RecordingAsyncBackend(http_response(200, corrupt, {"Content-Encoding": "gzip"})))
    result = await stack.call(_tool())

    assert result.startswith("Erro na requisição: falha ao chamar a tool (DecodingError)")
    assert "zlib" not in result.lower() and "header" not in result.lower()


# ── DNS que não responde ────────────────────────────────────────────


async def test_dns_travado_acima_do_executor_nao_acumula_pedidos_e_o_cliente_se_recupera():
    """Mais pedidos que threads do executor de DNS: todos saem por timeout, sem conectar; o cliente se recupera."""
    import threading

    import httpx

    from src.infrastructure.security.guarded_http import DNS_MAX_THREADS

    release = threading.Event()

    def stuck(host: str) -> list[str]:
        if host.startswith("preso"):
            release.wait(5)
        return [PUBLIC_IP]

    backend = RecordingAsyncBackend()
    transport = GuardedAsyncTransport(DestinationRule(), resolver=stuck, dns_timeout=0.1, network_backend=backend)
    try:
        async with httpx.AsyncClient(transport=transport) as client:
            outcomes = await asyncio.wait_for(
                asyncio.gather(
                    *(client.get(f"http://preso{i}.example.com/") for i in range(DNS_MAX_THREADS * 2 + 4)),
                    return_exceptions=True,
                ),
                5,
            )
            assert all(isinstance(outcome, httpx.ConnectTimeout) for outcome in outcomes)
            assert backend.connects == []
            release.set()
            for _ in range(50):  # as threads presas terminam e o executor volta a atender
                try:
                    assert (await client.get("http://livre.example.com/")).status_code == 200
                    break
                except httpx.ConnectTimeout:
                    await asyncio.sleep(0.05)
            else:
                pytest.fail("o executor de DNS não voltou a atender depois de soltar as threads presas")
    finally:
        release.set()
    assert backend.connects == [(PUBLIC_IP, 80)]


# ── backends: caminhos sem DNS ──────────────────────────────────────


async def test_socket_unix_nunca_e_destino_nem_no_backend_async_nem_no_sincrono():
    from src.infrastructure.security.guarded_http import GuardedSyncTransport
    from src.infrastructure.security.ssrf import BlockedDestinationError

    async_backend = GuardedAsyncTransport(DestinationRule(), network_backend=RecordingAsyncBackend())._pool
    sync_backend = GuardedSyncTransport(DestinationRule())._pool
    with pytest.raises(BlockedDestinationError):
        await async_backend._network_backend.connect_unix_socket("/var/run/docker.sock")
    with pytest.raises(BlockedDestinationError):
        sync_backend._network_backend.connect_unix_socket("/var/run/docker.sock")


def test_backend_sincrono_tenta_o_proximo_endereco_e_devolve_a_ultima_falha_se_todos_recusam():
    import httpx

    from src.infrastructure.security.guarded_http import GuardedSyncTransport
    from tests.fakes.network_backend import RecordingSyncBackend

    some = RecordingSyncBackend(refuse=("8.8.8.8",))
    transport = GuardedSyncTransport(
        DestinationRule(), resolver=lambda host: ["8.8.8.8", "8.8.4.4"], network_backend=some
    )
    with httpx.Client(transport=transport) as client:
        assert client.get("http://multi.example.com/").status_code == 200
    assert some.connects == [("8.8.8.8", 80), ("8.8.4.4", 80)]

    none = RecordingSyncBackend(refuse=("8.8.8.8", "8.8.4.4"))
    transport = GuardedSyncTransport(
        DestinationRule(), resolver=lambda host: ["8.8.8.8", "8.8.4.4"], network_backend=none
    )
    with httpx.Client(transport=transport) as client, pytest.raises(httpx.ConnectError):
        client.get("http://multi.example.com/")
    assert none.connects == [("8.8.8.8", 80), ("8.8.4.4", 80)]
