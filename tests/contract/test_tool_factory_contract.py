"""Contrato da porta ``IToolFactory`` (F4-02): as duas fontes reais de tool, HTTP e MCP, se comportam igual.

- lista vazia ⇒ nenhuma tool;
- tool da própria fonte ⇒ ``Function`` do agno com entrypoint async que devolve texto;
- tool da outra fonte ou que não monta ⇒ fica de fora com log de erro, sem exceção (o agente sobe sem ela);
- cada chamada ⇒ um evento de auditoria com a fonte, a tool, o resultado e a duração (F4-03).
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
import pytest
from agno.tools.function import Function

from src.domain.entities.tool import HttpMethod, Tool
from src.domain.ports import IToolFactory
from src.infrastructure.mcp_client.sessions import McpSessions, schema_hash
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from src.infrastructure.runtime.agno.mcp_tool_factory import McpToolFactory
from tests.fakes.logger import RecordingLogger
from tests.fakes.mcp_server import FAKE_MCP_URL, FakeMcpServer, mcp_tool

SEARCH = mcp_tool("search", "Busca issues")


def _http_tool(route: str = "https://api.example.com/cep") -> Tool:
    return Tool(id="cep", name="cep", description="Busca CEP", route=route, http_method=HttpMethod.GET, parameters=[])


def _mcp_tool(route: str = FAKE_MCP_URL) -> Tool:
    return Tool(
        id="github",
        name="GitHub",
        description="Servidor MCP",
        route=route,
        http_method=HttpMethod.GET,
        parameters=[],
        mcp_tools={"search": schema_hash(SEARCH.name, SEARCH.description, SEARCH.inputSchema)},
    )


@dataclass
class Source:
    factory: IToolFactory
    logger: RecordingLogger
    valid: Tool
    broken: Tool
    other: Tool


@pytest.fixture(params=["http", "mcp"])
async def source(request: pytest.FixtureRequest) -> AsyncIterator[Source]:
    logger = RecordingLogger()
    if request.param == "http":
        transport = httpx.MockTransport(lambda req: httpx.Response(200, json={"ok": 1}))
        http = HttpToolFactory(logger, transport=transport)
        # placeholder na rota sem parâmetro obrigatório: recusada na criação
        yield Source(http, logger, _http_tool(), _http_tool("https://api.example.com/cep/{cep}"), _mcp_tool())
        await http.aclose()
        return
    async with FakeMcpServer([SEARCH]) as server:
        sessions = McpSessions(logger, transport_factory=server.transport)
        broken = _mcp_tool()
        broken.mcp_tools = {"search": "0" * 64}  # hash que o servidor não confirma
        yield Source(McpToolFactory(sessions, logger), logger, _mcp_tool(), broken, _http_tool())
        await sessions.aclose()


async def test_lista_vazia_nao_cria_nada(source: Source):
    assert await source.factory.create_tools_from_configs([]) == []


async def test_tool_da_fonte_vira_function_async_que_devolve_texto(source: Source):
    [function] = await source.factory.create_tools_from_configs([source.valid])

    assert isinstance(function, Function)
    assert function.entrypoint is not None
    assert isinstance(await function.entrypoint(), str)


async def test_cada_chamada_gera_um_evento_de_auditoria(source: Source):
    [function] = await source.factory.create_tools_from_configs([source.valid])
    assert function.entrypoint is not None

    await function.entrypoint()
    await function.entrypoint()

    events = [r for r in source.logger.records if r.context.get("audit") == "tool_call"]
    assert [(e.context["tool_id"], e.context["function"], e.context["result"]) for e in events] == [
        (source.valid.id, function.name, "ok")
    ] * 2
    assert {e.context["source"] for e in events} == {"mcp" if source.valid.is_mcp else "http"}
    assert all(isinstance(e.context["duration_ms"], float) for e in events)


@pytest.mark.parametrize("kind", ["broken", "other"])
async def test_tool_que_nao_monta_fica_de_fora_com_log_sem_excecao(source: Source, kind: str):
    tool = source.broken if kind == "broken" else source.other

    assert await source.factory.create_tools_from_configs([tool]) == []
    assert [r for r in source.logger.records if r.level == "error"]
