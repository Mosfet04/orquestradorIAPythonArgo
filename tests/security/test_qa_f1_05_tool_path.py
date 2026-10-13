"""QA do F1-05 (segurança): parâmetro de rota hostil vindo do modelo não escapa do path da tool.

O valor do ``tool_call`` é entrada não confiável (prompt injection). A rota da tool é
``https://api.example.invalid/clientes/{id}/pedidos``: qualquer que seja o valor, o request
que sai deve ter host e esquema da rota, exatamente 3 segmentos, sem query nem fragmento
vindos do valor. ``.`` e ``..`` (o ``quote`` não codifica ponto e o httpx normalizaria o
segmento) são recusados antes do request desde a rodada 2 do F1-05.
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
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from tests.fakes import FakeChatModel, RecordingLogger

ROUTE_HOST = "api.example.invalid"

TOOL = Tool(
    id="pedidos",
    name="Pedidos",
    description="Lista pedidos",
    route=f"https://{ROUTE_HOST}/clientes/{{id}}/pedidos",
    http_method=HttpMethod.GET,
    parameters=[ToolParameter(name="id", type=ParameterType.STRING, description="Cliente", required=True)],
)

HOSTILE = [
    "a/b",
    "a/../../admin",
    "../admin",
    "/etc/passwd",
    "//evil.example.invalid/x",
    "%2F",
    "%2e%2e%2fadmin",
    "?x=1",
    "a?x=1&y=2",
    "#f",
    "a#f",
    "",
    " ",
    "ção/ü?",
    "a\\b",
    "a\r\nHost: evil.example.invalid",
    "a\x00b",
    "@evil.example.invalid",
    "evil.example.invalid:80/",
    "{id}",
    "{",
    "x" * 4000,
]
DOT_SEGMENTS = [".", ".."]


async def _run_tool(value: Any) -> tuple[list[httpx.Request], list[str], RecordingLogger]:
    """Roda o agente com um ``tool_call`` para ``value``; devolve requests, resultados de tool e logger."""
    seen: list[httpx.Request] = []
    real_client: Callable[..., httpx.AsyncClient] = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"ok": True})

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    call = ModelResponse(
        role="assistant",
        tool_calls=[
            {
                "id": "c1",
                "type": "function",
                "function": {"name": "pedidos", "arguments": json.dumps({"id": value})},
            }
        ],
    )
    logger = RecordingLogger()
    functions = await HttpToolFactory(logger=logger).create_tools_from_configs([TOOL])
    model = FakeChatModel(responses=[call, "fim"])
    agent = Agent(id="a", model=model, tools=functions, telemetry=False)
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(httpx, "AsyncClient", client)
        await agent.arun("pedidos")
    results = [content for role, content in model.calls[1].messages if role == "tool"]
    return seen, results, logger


async def _send(value: Any) -> httpx.Request:
    seen, _, logger = await _run_tool(value)
    assert len(seen) == 1, f"o request da tool não saiu para {value!r}: {logger.records}"
    return seen[0]


@pytest.mark.parametrize("value", DOT_SEGMENTS)
async def test_segmento_de_ponto_no_parametro_de_rota_e_recusado_sem_request(value: str):
    seen, results, _ = await _run_tool(value)

    assert seen == []
    assert results == [
        "Erro nos argumentos da tool pedidos: valor inválido para id: '.' e '..' não são permitidos na rota"
    ]


@pytest.mark.parametrize("value", HOSTILE, ids=lambda v: repr(v)[:30])
async def test_valor_hostil_no_parametro_de_rota_nao_escapa_do_path_da_tool(value: str):
    request = await _send(value)

    assert request.url.scheme == "https"
    assert request.url.host == ROUTE_HOST
    assert request.url.port is None
    raw_path = request.url.raw_path.decode("ascii")
    assert "?" not in raw_path, "query injetada pelo valor"
    assert request.url.query == b"", "query injetada pelo valor"
    assert request.url.fragment == ""
    segments = raw_path.split("/")
    assert segments[0] == "" and segments[1] == "clientes" and segments[-1] == "pedidos", raw_path
    assert len(segments) == 4, f"o valor criou/removeu segmentos de path: {raw_path}"
    assert "evil.example.invalid" not in str(request.headers)


@pytest.mark.parametrize("value", ["a/b", "?x=1", "#f", "%2F", "ção"])
async def test_valor_e_codificado_por_inteiro_e_decodifica_de_volta_ao_original(value: str):
    from urllib.parse import unquote

    request = await _send(value)

    segment = request.url.raw_path.decode("ascii").split("/")[2]
    assert unquote(segment) == value
    assert "/" not in segment and "?" not in segment and "#" not in segment
