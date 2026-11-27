"""F4-02: ``Function`` do agno para cada tool MCP da allowlist e o despacho HTTP/MCP pela config da tool."""

from __future__ import annotations

from typing import Any

from agno.tools.function import Function

from src.domain.entities.tool import HttpMethod, Tool
from src.domain.ports import IToolFactory
from src.infrastructure.mcp_client.sessions import McpSessions, schema_hash
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from src.infrastructure.runtime.agno.mcp_tool_factory import McpToolFactory, ToolFactoryBySource
from tests.fakes.logger import RecordingLogger
from tests.fakes.mcp_server import FAKE_MCP_URL, FakeMcpServer, mcp_tool

SEARCH = mcp_tool("search", "Busca issues", {"q": {"type": "string", "description": "texto"}})
CLOSE = mcp_tool("close_issue", "Fecha a issue")


def _hash(tool: Any) -> str:
    return schema_hash(tool.name, tool.description, tool.inputSchema)


def _mcp(
    allowlist: dict[str, str], instructions: str | None = None, route: str = FAKE_MCP_URL, tool_id: str = "github"
) -> Tool:
    return Tool(
        id=tool_id,
        name="GitHub",
        description="Servidor MCP",
        route=route,
        http_method=HttpMethod.GET,
        parameters=[],
        instructions=instructions,
        mcp_tools=allowlist,
    )


def _http() -> Tool:
    return Tool(
        id="cep", name="cep", description="Busca CEP", route="https://api.example.com/cep",
        http_method=HttpMethod.GET, parameters=[],
    )


async def test_uma_function_por_tool_da_allowlist_com_o_schema_do_servidor():
    async with FakeMcpServer([SEARCH, CLOSE, mcp_tool("delete_repo")]) as server:
        sessions = McpSessions(RecordingLogger(), transport_factory=server.transport)
        factory = McpToolFactory(sessions, RecordingLogger())
        try:
            functions = await factory.create_tools_from_configs(
                [_mcp({"search": _hash(SEARCH), "close_issue": _hash(CLOSE)}, instructions="Só leia issues")]
            )
            assert all(isinstance(f, Function) for f in functions)
            server.results["search"] = "2 issues"
            assert functions[0].entrypoint is not None
            result = await functions[0].entrypoint(q="bug")
        finally:
            await sessions.aclose()

    assert [(f.name, f.description, f.parameters) for f in functions] == [
        ("github__search", "Busca issues", SEARCH.inputSchema),
        ("github__close_issue", "Fecha a issue", CLOSE.inputSchema),
    ]
    assert [f.instructions for f in functions] == ["Instruções da tool github: Só leia issues", None]
    assert [f.add_instructions for f in functions] == [True, False]
    assert result == "2 issues"
    assert server.calls == [("search", {"q": "bug"})]


async def test_servidor_que_nao_abre_deixa_o_agente_sem_a_tool_com_log():
    logger = RecordingLogger()
    async with FakeMcpServer([SEARCH]) as server:
        server.fail_posts = 1
        sessions = McpSessions(RecordingLogger(), transport_factory=server.transport)
        try:
            factory = McpToolFactory(sessions, logger)
            functions = await factory.create_tools_from_configs([_mcp({"search": _hash(SEARCH)})])
        finally:
            await sessions.aclose()

    assert functions == []
    assert [(r.level, r.message, r.context) for r in logger.records] == [
        (
            "error",
            "Tool MCP recusada: servidor MCP não abriu",
            {
                "tool_id": "github",
                "reason": "servidor MCP indisponível (HTTPStatusError)",
                "error_type": "HTTPStatusError",
            },
        )
    ]


OUT_OF_PATTERN = "Tool MCP recusada: nome exposto fora de [A-Za-z0-9_-]{1,64}"


async def test_nome_exposto_fora_do_padrao_do_provider_e_recusado_com_log():
    logger = RecordingLogger()
    async with FakeMcpServer([SEARCH, CLOSE]) as server:
        sessions = McpSessions(RecordingLogger(), transport_factory=server.transport)
        factory = McpToolFactory(sessions, logger)
        try:
            long_id = await factory.create_tools_from_configs(
                [_mcp({"search": _hash(SEARCH), "close_issue": _hash(CLOSE)}, "Só leia", tool_id="x" * 60)]
            )
            accented = await factory.create_tools_from_configs([_mcp({"search": _hash(SEARCH)}, tool_id="ação")])
        finally:
            await sessions.aclose()

    assert long_id == accented == []
    assert [(r.level, r.message, r.context) for r in logger.records] == [
        ("error", OUT_OF_PATTERN, {"tool_id": "x" * 60, "mcp_tool": "search"}),
        ("error", OUT_OF_PATTERN, {"tool_id": "x" * 60, "mcp_tool": "close_issue"}),
        ("error", OUT_OF_PATTERN, {"tool_id": "ação", "mcp_tool": "search"}),
    ]


async def test_instrucoes_vao_na_primeira_function_aceita():
    tool_id = "x" * 52  # x..x__search cabe em 64; x..x__close_issue não
    async with FakeMcpServer([SEARCH, CLOSE]) as server:
        sessions = McpSessions(RecordingLogger(), transport_factory=server.transport)
        factory = McpToolFactory(sessions, RecordingLogger())
        try:
            functions = await factory.create_tools_from_configs(
                [_mcp({"close_issue": _hash(CLOSE), "search": _hash(SEARCH)}, "Só leia", tool_id=tool_id)]
            )
        finally:
            await sessions.aclose()

    assert [(f.name, f.instructions, f.add_instructions) for f in functions] == [
        (f"{tool_id}__search", f"Instruções da tool {tool_id}: Só leia", True)
    ]


class _RecordingFactory(IToolFactory):
    def __init__(self, label: str) -> None:
        self.label = label
        self.received: list[list[str]] = []

    async def create_tools_from_configs(self, tools: list[Tool]) -> list[Any]:
        self.received.append([t.id for t in tools])
        return [f"{self.label}:{t.id}" for t in tools]


async def test_despacho_manda_cada_tool_para_a_fabrica_da_fonte_dela():
    http, mcp = _RecordingFactory("http"), _RecordingFactory("mcp")
    router = ToolFactoryBySource(http=http, mcp=mcp)

    assert await router.create_tools_from_configs([_http()]) == ["http:cep"]
    assert await router.create_tools_from_configs([_mcp({"search": _hash(SEARCH)})]) == ["mcp:github"]
    assert http.received == [["cep"]]
    assert mcp.received == [["github"]]


async def test_cada_fabrica_recusa_a_tool_da_outra_fonte():
    logger = RecordingLogger()
    sessions = McpSessions(RecordingLogger(), transport_factory=lambda: None)  # type: ignore[arg-type,return-value]

    assert await HttpToolFactory(logger).create_tools_from_configs([_mcp({"search": _hash(SEARCH)})]) == []
    assert await McpToolFactory(sessions, logger).create_tools_from_configs([_http()]) == []
    assert [(r.level, r.message, r.context) for r in logger.records] == [
        ("error", "Tool MCP recusada pela fábrica HTTP (vai pela fábrica MCP)", {"tool_id": "github"}),
        ("error", "Tool HTTP recusada pela fábrica MCP (vai pela fábrica HTTP)", {"tool_id": "cep"}),
    ]
