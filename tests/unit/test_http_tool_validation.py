"""Validação dos argumentos da tool HTTP antes do request (F1-05, rodada 2).

Com ``skip_entrypoint_processing=True`` o agno não valida nada em runtime: o schema
é só o que o modelo vê. ``http_function`` confere chaves, obrigatórios e tipo básico
e, em violação, devolve erro curto ao modelo sem chamar o upstream. O upstream é um
``httpx.MockTransport``: nada sai da máquina.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse

from src.domain.entities.tool import HttpMethod, ParameterType, Tool, ToolParameter
from src.infrastructure.http.http_tool_factory import HttpToolFactory
from tests.fakes import FakeChatModel, LoggedRecord, RecordingLogger

TOOL = Tool(
    id="busca",
    name="Busca",
    description="Busca pedidos",
    route="https://api.example.invalid/clientes/{cliente_id}/pedidos",
    http_method=HttpMethod.GET,
    parameters=[
        ToolParameter(name="cliente_id", type=ParameterType.STRING, description="Cliente", required=True),
        ToolParameter(name="pagina", type=ParameterType.INTEGER, description="Página"),
        ToolParameter(name="valor", type=ParameterType.FLOAT, description="Valor"),
        ToolParameter(name="abertos", type=ParameterType.BOOLEAN, description="Abertos"),
        ToolParameter(name="tags", type=ParameterType.ARRAY, description="Tags"),
        ToolParameter(name="filtro", type=ParameterType.OBJECT, description="Filtro"),
    ],
)


class Upstream:
    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.handler: Callable[[httpx.Request], httpx.Response] = lambda r: httpx.Response(200, json={"ok": True})


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> Upstream:
    state = Upstream()
    real_client: Callable[..., httpx.AsyncClient] = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        state.requests.append(request)
        return state.handler(request)

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return state


async def _call(logger: RecordingLogger, tool: Tool = TOOL, **arguments: Any) -> str:
    (function,) = await HttpToolFactory(logger=logger).create_tools_from_configs([tool])
    result: str = await function.entrypoint(**arguments)
    return result


def _warnings(logger: RecordingLogger) -> list[tuple[str, dict[str, Any]]]:
    return [(r.message, r.context) for r in logger.records if r.level == "warning"]


async def test_argumentos_validos_chamam_o_upstream(upstream: Upstream):
    logger = RecordingLogger()

    result = await _call(
        logger,
        cliente_id="42",
        pagina=2,
        valor=3,  # integer é number válido
        abertos=False,
        tags=["a"],
        filtro={"k": "v"},
    )

    assert result == "{'ok': True}"
    (request,) = upstream.requests
    assert request.url.path == "/clientes/42/pedidos"
    assert _warnings(logger) == []


async def test_argumento_nao_declarado_nao_chama_o_upstream(upstream: Upstream):
    logger = RecordingLogger()

    result = await _call(logger, cliente_id="42", admin="valor-sensivel", url="http://evil.invalid")

    assert result == "Erro nos argumentos da tool busca: argumento não declarado: admin, url"
    assert upstream.requests == []
    assert _warnings(logger) == [
        ("Argumentos inválidos no tool call", {"tool_id": "busca", "arguments": ["admin", "url"]})
    ]
    assert "valor-sensivel" not in repr(logger.records)


async def test_argumento_obrigatorio_ausente_nao_chama_o_upstream(upstream: Upstream):
    logger = RecordingLogger()

    result = await _call(logger, pagina=1)

    assert result == "Erro nos argumentos da tool busca: argumento obrigatório ausente: cliente_id"
    assert upstream.requests == []
    assert _warnings(logger) == [
        ("Argumentos inválidos no tool call", {"tool_id": "busca", "arguments": ["cliente_id"]})
    ]


@pytest.mark.parametrize(
    ("name", "value", "expected"),
    [
        ("cliente_id", 42, "string"),
        ("pagina", "abc", "integer"),
        ("pagina", True, "integer"),  # bool não conta como integer
        ("pagina", 1.5, "integer"),
        ("valor", "1.5", "number"),
        ("valor", False, "number"),
        ("abertos", "true", "boolean"),
        ("abertos", 1, "boolean"),
        ("tags", "a,b", "array"),
        ("filtro", ["k"], "object"),
    ],
)
async def test_tipo_invalido_nao_chama_o_upstream(upstream: Upstream, name: str, value: Any, expected: str):
    logger = RecordingLogger()
    arguments: dict[str, Any] = {"cliente_id": "42", name: value}

    result = await _call(logger, **arguments)

    assert result == f"Erro nos argumentos da tool busca: tipo inválido para {name}: esperado {expected}"
    assert upstream.requests == []
    # registro único e exato: só o nome do argumento, nunca o valor
    assert logger.records == [
        LoggedRecord("warning", "Argumentos inválidos no tool call", {"tool_id": "busca", "arguments": [name]})
    ]


async def test_null_em_opcional_conta_como_ausente_e_em_obrigatorio_e_erro(upstream: Upstream):
    logger = RecordingLogger()

    ok = await _call(logger, cliente_id="42", pagina=None)
    missing = await _call(logger, cliente_id=None)

    assert ok == "{'ok': True}"
    (request,) = upstream.requests
    assert request.url.params.multi_items() == []
    assert missing == "Erro nos argumentos da tool busca: argumento obrigatório ausente: cliente_id"


@pytest.mark.parametrize("value", [".", ".."])
async def test_segmento_de_ponto_em_parametro_de_rota_e_recusado(upstream: Upstream, value: str):
    logger = RecordingLogger()

    result = await _call(logger, cliente_id=value)

    assert result == (
        "Erro nos argumentos da tool busca: valor inválido para cliente_id: '.' e '..' não são permitidos na rota"
    )
    assert upstream.requests == []
    assert _warnings(logger) == [
        ("Argumentos inválidos no tool call", {"tool_id": "busca", "arguments": ["cliente_id"]})
    ]


@pytest.mark.parametrize("value", ["...", ".a", "a.", "%2e%2e"])
async def test_ponto_que_nao_e_segmento_inteiro_passa(upstream: Upstream, value: str):
    result = await _call(RecordingLogger(), cliente_id=value)

    assert result == "{'ok': True}"
    assert len(upstream.requests) == 1


# ── placeholders da rota na criação ─────────────────────────────────


def _tool_with_route(route: str, *params: ToolParameter) -> Tool:
    return Tool(
        id="rota",
        name="Rota",
        description="Rota com placeholders",
        route=route,
        http_method=HttpMethod.GET,
        parameters=list(params),
    )


@pytest.mark.parametrize(
    ("params", "bad"),
    [
        ((), ["a", "b"]),
        ((ToolParameter(name="a", type=ParameterType.STRING, description="a", required=True),), ["b"]),
        (
            (
                ToolParameter(name="a", type=ParameterType.STRING, description="a", required=True),
                ToolParameter(name="b", type=ParameterType.STRING, description="b"),
            ),
            ["b"],
        ),
    ],
)
async def test_placeholder_da_rota_nao_declarado_ou_opcional_recusa_a_tool(params: tuple[ToolParameter, ...], bad):
    logger = RecordingLogger()
    tool = _tool_with_route("https://api.example.invalid/{a}/x/{b}", *params)

    created = await HttpToolFactory(logger=logger).create_tools_from_configs([tool])

    assert created == []
    assert [(r.message, r.context) for r in logger.records if r.level == "error"] == [
        (
            "Tool recusada: placeholder da rota sem parâmetro obrigatório declarado",
            {"tool_id": "rota", "placeholders": bad},
        )
    ]


async def test_placeholders_declarados_e_obrigatorios_criam_a_tool():
    logger = RecordingLogger()
    tool = _tool_with_route(
        "https://api.example.invalid/{a}/x/{b}",
        ToolParameter(name="a", type=ParameterType.STRING, description="a", required=True),
        ToolParameter(name="b", type=ParameterType.INTEGER, description="b", required=True),
    )

    (function,) = await HttpToolFactory(logger=logger).create_tools_from_configs([tool])

    assert function.name == "rota"
    assert logger.messages("error") == []


# ── falha de rede: mensagem útil ao modelo ──────────────────────────


@pytest.mark.parametrize("exc_type", [httpx.ReadTimeout, httpx.ConnectTimeout, httpx.PoolTimeout])
async def test_timeout_diz_ao_modelo_que_foi_timeout(upstream: Upstream, exc_type: type[httpx.TimeoutException]):
    logger = RecordingLogger()

    def boom(request: httpx.Request) -> httpx.Response:
        raise exc_type("", request=request)

    upstream.handler = boom

    result = await _call(logger, cliente_id="42")

    assert result == f"Erro na requisição: timeout ao chamar a tool ({exc_type.__name__})"
    assert [(r.message, r.context) for r in logger.records if r.level == "error"] == [
        ("Request error", {"tool_id": "busca", "error_type": exc_type.__name__, "error": exc_type.__name__})
    ]


async def test_erro_de_rede_sem_mensagem_usa_o_tipo_da_excecao(upstream: Upstream):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("", request=request)

    upstream.handler = boom

    result = await _call(RecordingLogger(), cliente_id="42")

    assert result == "Erro na requisição: ConnectError"


# ── coerção do agno: "true"/"false" viram bool antes do entrypoint ──


def _tool_call(name: str, arguments: dict[str, Any]) -> ModelResponse:
    return ModelResponse(
        role="assistant",
        tool_calls=[{"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}],
    )


def _filtro_tool(method: HttpMethod) -> Tool:
    return Tool(
        id="filtro",
        name="Filtro",
        description="Filtra",
        route="https://api.example.invalid/itens",
        http_method=method,
        parameters=[ToolParameter(name="ativo", type=ParameterType.STRING, description="Texto livre", required=True)],
    )


@pytest.mark.parametrize("raw", ["true", "False"])
@pytest.mark.parametrize("method", [HttpMethod.GET, HttpMethod.POST])
async def test_string_true_false_convertida_em_bool_pelo_agno_volta_a_ser_string(
    upstream: Upstream, method: HttpMethod, raw: str
):
    logger = RecordingLogger()
    functions = await HttpToolFactory(logger=logger).create_tools_from_configs([_filtro_tool(method)])
    model = FakeChatModel(responses=[_tool_call("filtro", {"ativo": raw}), "fim"])
    agent = Agent(id="a", model=model, tools=functions, telemetry=False)

    await agent.arun("filtre")

    (request,) = upstream.requests
    if method is HttpMethod.GET:
        assert request.url.params.multi_items() == [("ativo", raw.lower())]
    else:
        assert json.loads(request.content) == {"ativo": raw.lower()}
    assert _warnings(logger) == []


# ── parâmetros declarados com nome de opção do httpx ────────────────


@pytest.mark.parametrize("method", [HttpMethod.GET, HttpMethod.POST])
async def test_parametro_declarado_com_nome_de_opcao_do_httpx_vai_como_dado(upstream: Upstream, method: HttpMethod):
    tool = Tool(
        id="opcoes",
        name="Opções",
        description="Parâmetros com nomes de opção do httpx",
        route="https://api.example.invalid/recurso",
        http_method=method,
        parameters=[
            ToolParameter(name="url", type=ParameterType.STRING, description="url", required=True),
            ToolParameter(name="method", type=ParameterType.STRING, description="method", required=True),
            ToolParameter(name="headers", type=ParameterType.STRING, description="headers", required=True),
            ToolParameter(name="follow_redirects", type=ParameterType.BOOLEAN, description="fr", required=True),
        ],
    )
    arguments = {
        "url": "http://evil.example.invalid/",
        "method": "DELETE",
        "headers": "X-Evil: 1",
        "follow_redirects": True,
    }

    result = await _call(RecordingLogger(), tool, **arguments)

    assert result == "{'ok': True}"
    (request,) = upstream.requests
    assert (request.method, request.url.scheme, request.url.host, request.url.path) == (
        method.value,
        "https",
        "api.example.invalid",
        "/recurso",
    )
    assert "x-evil" not in request.headers
    if method is HttpMethod.GET:
        assert dict(request.url.params) == {
            "url": "http://evil.example.invalid/",
            "method": "DELETE",
            "headers": "X-Evil: 1",
            "follow_redirects": "true",
        }
    else:
        assert json.loads(request.content) == arguments


# ── nomes desconhecidos: limite na mensagem e no log ────────────────


async def test_nomes_nao_declarados_limitados_a_10_e_truncados_a_64(upstream: Upstream):
    logger = RecordingLogger()
    longo = "z" * 100
    extras: dict[str, Any] = {f"x{i:02d}": 1 for i in range(14)}
    extras["a" + longo] = 1

    result = await _call(logger, cliente_id="42", **extras)

    expected = ["a" + "z" * 63, *[f"x{i:02d}" for i in range(9)], "..."]
    assert result == f"Erro nos argumentos da tool busca: argumento não declarado: {', '.join(expected)}"
    assert upstream.requests == []
    assert logger.records == [
        LoggedRecord("warning", "Argumentos inválidos no tool call", {"tool_id": "busca", "arguments": expected})
    ]
