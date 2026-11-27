"""QA do F4-02: bordas da fábrica de tools MCP (nome exposto no limite de 64, entrypoint, servidor fora do ar)."""

from __future__ import annotations

from agno.tools.function import Function

from src.domain.entities.tool import HttpMethod, Tool
from src.infrastructure.mcp_client.sessions import McpServerError, McpSessions, schema_hash
from src.infrastructure.runtime.agno.mcp_tool_factory import McpToolFactory
from tests.fakes.logger import RecordingLogger
from tests.fakes.mcp_server import FAKE_MCP_URL, FakeMcpServer, mcp_tool

SEARCH = mcp_tool("search", "Busca issues", {"q": {"type": "string"}})


def _mcp(tool_id: str, allowlist: dict[str, str]) -> Tool:
    return Tool(
        id=tool_id, name="GitHub", description="Servidor MCP", route=FAKE_MCP_URL,
        http_method=HttpMethod.GET, parameters=[], mcp_tools=allowlist,
    )


async def test_nome_exposto_com_exatamente_64_caracteres_e_aceito_e_com_65_e_recusado():
    pinned = {"search": schema_hash(SEARCH.name, SEARCH.description, SEARCH.inputSchema)}
    logger = RecordingLogger()
    async with FakeMcpServer([SEARCH]) as server:
        sessions = McpSessions(RecordingLogger(), transport_factory=server.transport)
        factory = McpToolFactory(sessions, logger)
        try:
            at_limit = await factory.create_tools_from_configs([_mcp("a" * 56, pinned)])  # 56 + 2 + 6 = 64
            over = await factory.create_tools_from_configs([_mcp("a" * 57, pinned)])  # 65
        finally:
            await sessions.aclose()

    assert [f.name for f in at_limit] == ["a" * 56 + "__search"] and len(at_limit[0].name) == 64
    assert over == []
    assert [r.context for r in logger.records if r.level == "error"] == [{"tool_id": "a" * 57, "mcp_tool": "search"}]


async def test_entrypoint_da_function_repassa_so_os_argumentos_do_modelo_e_o_nome_do_servidor():
    pinned = {"search": schema_hash(SEARCH.name, SEARCH.description, SEARCH.inputSchema)}
    async with FakeMcpServer([SEARCH]) as server:
        sessions = McpSessions(RecordingLogger(), transport_factory=server.transport)
        factory = McpToolFactory(sessions, RecordingLogger())
        try:
            [function] = await factory.create_tools_from_configs([_mcp("gh", pinned)])
            assert isinstance(function, Function) and function.entrypoint is not None
            result = await function.entrypoint(q="bug", extra={"a": 1})
        finally:
            await sessions.aclose()

    assert result == "ok search"
    assert server.calls == [("search", {"q": "bug", "extra": {"a": 1}})]  # nome do servidor, sem o prefixo do id


async def test_agente_com_dois_servidores_um_fora_do_ar_monta_o_outro():
    pinned = {"search": schema_hash(SEARCH.name, SEARCH.description, SEARCH.inputSchema)}
    logger = RecordingLogger()
    async with FakeMcpServer([SEARCH]) as server:
        class Routed(McpSessions):
            async def _open(self, tool: Tool):  # type: ignore[no-untyped-def]
                if tool.id == "caido":
                    raise McpServerError("servidor MCP indisponível (ConnectError)", "ConnectError")
                return await super()._open(tool)

        sessions = Routed(RecordingLogger(), transport_factory=server.transport)
        factory = McpToolFactory(sessions, logger)
        try:
            functions = await factory.create_tools_from_configs([_mcp("caido", pinned), _mcp("ok", pinned)])
        finally:
            await sessions.aclose()

    assert [f.name for f in functions] == ["ok__search"]
    assert [(r.message, r.context["tool_id"]) for r in logger.records if r.level == "error"] == [
        ("Tool MCP recusada: servidor MCP não abriu", "caido")
    ]
