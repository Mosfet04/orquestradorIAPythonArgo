"""QA do F2-09: ordem das checagens de argumento da tool HTTP depois do refactor de ``_validated_arguments``.

O refactor dividiu a função em ``_reject_unknown`` -> ``_present_arguments`` -> ``_reject_missing`` ->
``_reject_wrong_types`` -> ``_reject_dot_segments``. Os testes de ``test_http_tool_validation.py``
cobrem cada violação isolada; aqui, o argumento que viola mais de uma regra: o erro que o modelo
recebe (e o que vai ao log) é o da primeira regra, e o upstream nunca é chamado.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from src.domain.entities.tool import HttpMethod, ParameterType, Tool, ToolParameter
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from tests.fakes import LoggedRecord, RecordingLogger

PREFIX = "Erro nos argumentos da tool t: "


def _tool(route: str, *params: ToolParameter, method: HttpMethod = HttpMethod.GET) -> Tool:
    return Tool(id="t", name="T", description="d", route=route, http_method=method, parameters=list(params))


def _param(name: str, kind: ParameterType = ParameterType.STRING, *, required: bool = False) -> ToolParameter:
    return ToolParameter(name=name, type=kind, description="d", required=required)


ROUTE = "https://api.example.invalid/c/{id}"
TOOL = _tool(
    ROUTE,
    _param("id", required=True),
    _param("page", ParameterType.INTEGER),
    _param("price", ParameterType.FLOAT),
    _param("flag", ParameterType.BOOLEAN),
)
TWO_REQUIRED = _tool(
    "https://api.example.invalid/{a}/{b}",
    _param("a", required=True),
    _param("b", required=True),
    _param("q"),
)
TWO_REQUIRED_REVERSED = _tool(
    "https://api.example.invalid/{b}/{a}",
    _param("a", required=True),
    _param("b", required=True),
)


@pytest.fixture
def upstream_requests(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    requests: list[httpx.Request] = []
    real_client: Callable[..., httpx.AsyncClient] = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return requests


async def _call(tool: Tool, logger: RecordingLogger, **arguments: Any) -> str:
    (function,) = await HttpToolFactory(logger=logger).create_tools_from_configs([tool])
    result: str = await function.entrypoint(**arguments)
    return result


@pytest.mark.parametrize(
    ("tool", "arguments", "message", "logged"),
    [
        # não declarado vence ausente, tipo e ponto
        (TOOL, {"admin": 1, "page": "x"}, "argumento não declarado: admin", ["admin"]),
        (TOOL, {"id": "..", "page": "x", "admin": 1}, "argumento não declarado: admin", ["admin"]),
        # não declarado com valor None continua não declarado (a checagem vem antes do descarte de None)
        (TOOL, {"id": "1", "admin": None}, "argumento não declarado: admin", ["admin"]),
        # ausente vence tipo e ponto
        (TOOL, {"page": "x"}, "argumento obrigatório ausente: id", ["id"]),
        (TOOL, {"id": None, "page": "x"}, "argumento obrigatório ausente: id", ["id"]),
        # tipo vence ponto (id é string ".." válida; page quebra primeiro)
        (TOOL, {"id": "..", "page": "x"}, "tipo inválido para page: esperado integer", ["page"]),
        (TOOL, {"id": ".", "flag": 1}, "tipo inválido para flag: esperado boolean", ["flag"]),
        # ponto sozinho
        (TOOL, {"id": ".."}, "valor inválido para id: '.' e '..' não são permitidos na rota", ["id"]),
        # vários ausentes: ordem de ``Tool.parameters`` e não a dos kwargs
        (TWO_REQUIRED, {"q": "x"}, "argumento obrigatório ausente: a, b", ["a", "b"]),
        (TWO_REQUIRED_REVERSED, {}, "argumento obrigatório ausente: a, b", ["a", "b"]),
        # vários com tipo errado: o primeiro dos argumentos recebidos (ordem dos kwargs)
        (TOOL, {"id": "1", "page": "x", "price": "y"}, "tipo inválido para page: esperado integer", ["page"]),
        (TOOL, {"id": "1", "price": "y", "page": "x"}, "tipo inválido para price: esperado number", ["price"]),
        # vários segmentos de ponto: o primeiro placeholder da rota, não o primeiro kwarg
        (
            TWO_REQUIRED,
            {"b": "..", "a": "."},
            "valor inválido para a: '.' e '..' não são permitidos na rota",
            ["a"],
        ),
        (
            TWO_REQUIRED_REVERSED,
            {"a": "..", "b": "."},
            "valor inválido para b: '.' e '..' não são permitidos na rota",
            ["b"],
        ),
    ],
    ids=lambda v: v if isinstance(v, str) and " " in v else "",
)
async def test_argumento_que_viola_mais_de_uma_regra_recebe_o_erro_da_primeira(
    upstream_requests: list[httpx.Request],
    tool: Tool,
    arguments: dict[str, Any],
    message: str,
    logged: list[str],
) -> None:
    logger = RecordingLogger()

    result = await _call(tool, logger, **arguments)

    assert result == PREFIX + message
    assert upstream_requests == []
    assert logger.records == [
        LoggedRecord("warning", "Argumentos inválidos no tool call", {"tool_id": "t", "arguments": logged})
    ]


async def test_nao_declarados_sao_listados_em_ordem_alfabetica_e_nao_na_dos_kwargs(
    upstream_requests: list[httpx.Request],
) -> None:
    result = await _call(TOOL, RecordingLogger(), id="1", zeta=1, alfa=2, mid=3)

    assert result == PREFIX + "argumento não declarado: alfa, mid, zeta"
    assert upstream_requests == []


async def test_booleano_em_string_declarado_vira_texto_antes_da_checagem_de_tipo(
    upstream_requests: list[httpx.Request],
) -> None:
    """``_present_arguments`` desfaz o bool do agno; sem isso o ``True`` seria "tipo inválido" para string."""
    logger = RecordingLogger()

    result = await _call(TOOL, logger, id=True, flag=False)

    assert result == "{'ok': True}"
    (request,) = upstream_requests
    assert request.url.path == "/c/true"  # id="true" na rota
    assert logger.records[-1].message == "HTTP OK"


async def test_ponto_so_vale_em_parametro_de_rota_e_none_opcional_nao_vira_valor(
    upstream_requests: list[httpx.Request],
) -> None:
    tool = _tool(ROUTE, _param("id", required=True), _param("q"), _param("r"))

    result = await _call(tool, RecordingLogger(), id="1", q="..", r=None)

    assert result == "{'ok': True}"
    (request,) = upstream_requests
    assert (request.url.path, dict(request.url.params)) == ("/c/1", {"q": ".."})
