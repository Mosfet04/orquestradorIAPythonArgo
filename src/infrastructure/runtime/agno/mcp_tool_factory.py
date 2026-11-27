"""Tools MCP no agno (F4-02) e o despacho de cada tool para a fábrica da fonte dela (HTTP ou MCP).

``McpToolFactory``: uma ``Function`` por tool da allowlist (``Tool.mcp_tools``) que o servidor lista com o hash
fixado (``McpSessions.pinned_tools``), com nome ``<id da tool>__<nome no servidor>`` (dois servidores com a mesma
tool não colidem), recusado com log se sair de ``[A-Za-z0-9_-]{1,64}`` (o provider recusaria o request inteiro;
nome repetido no agente é conferido onde o agente junta as tools, ``AgentFactoryService``), descrição e
``parameters`` do servidor (já conferidos pelo hash) e entrypoint async (auditado, F4-03) que chama
``McpSessions.call_with_outcome``.
``instructions`` da config vão no system message uma vez (na primeira função aceita).
``skip_entrypoint_processing``: como nas tools HTTP, sem isso o agno recalcula o schema pela assinatura
(``**kwargs``); os argumentos vão ao servidor como vieram do modelo, e o servidor os valida pelo próprio schema.

``ToolFactoryBySource``: a ``IToolFactory`` do agente, com as duas implementações reais da porta (HTTP e MCP).
"""

from __future__ import annotations

from typing import Any

from agno.tools.function import Function

from src.domain.entities.tool import MCP_TOOL_NAME, Tool
from src.domain.ports import ILogger, IToolFactory
from src.infrastructure.mcp_client.sessions import McpServerError, McpSessions, PinnedTool
from src.infrastructure.runtime.agno.http_tool_factory import build_instructions
from src.infrastructure.runtime.agno.tool_audit import audited_entrypoint
from src.infrastructure.tools.call_outcome import ToolCallOutcome


class McpToolFactory(IToolFactory):
    """Cria as ``Function`` das tools MCP; servidor que não abre deixa o agente sem elas, com log."""

    def __init__(self, sessions: McpSessions, logger: ILogger) -> None:
        self._sessions = sessions
        self._logger = logger

    async def create_tools_from_configs(self, tools: list[Tool]) -> list[Any]:
        """Funções das tools MCP conferidas; a tool HTTP é recusada (vai pela fábrica HTTP)."""
        functions: list[Function] = []
        for tool in tools:
            if not tool.is_mcp:
                self._logger.error("Tool HTTP recusada pela fábrica MCP (vai pela fábrica HTTP)", tool_id=tool.id)
                continue
            try:
                pinned = await self._sessions.pinned_tools(tool)
            except McpServerError as exc:
                self._logger.error(
                    "Tool MCP recusada: servidor MCP não abriu",
                    tool_id=tool.id,
                    reason=exc.reason,
                    error_type=exc.error_type,
                )
                continue
            first = True
            for item in pinned:
                name = f"{tool.id}__{item.name}"
                if not MCP_TOOL_NAME.fullmatch(name):
                    self._logger.error(
                        "Tool MCP recusada: nome exposto fora de [A-Za-z0-9_-]{1,64}",
                        tool_id=tool.id,
                        mcp_tool=item.name,
                    )
                    continue
                functions.append(self._function(tool, item, name, first=first))
                first = False
        return functions

    def _function(self, tool: Tool, item: PinnedTool, exposed_name: str, *, first: bool) -> Function:
        sessions = self._sessions
        name = item.name

        async def mcp_call(kwargs: dict[str, Any]) -> ToolCallOutcome:
            """Chama a tool no servidor MCP."""
            return await sessions.call_with_outcome(tool, name, kwargs)

        properties = item.input_schema.get("properties")

        instructions = build_instructions(tool) if first else None
        return Function(
            name=exposed_name,
            description=item.description,
            parameters=item.input_schema,
            instructions=instructions,
            add_instructions=instructions is not None,
            entrypoint=audited_entrypoint(
                mcp_call,
                logger=self._logger,
                source="mcp",
                tool_id=tool.id,
                function_name=exposed_name,
                declared_arguments=properties if isinstance(properties, dict) else (),
                mcp_tool=name,
            ),
            skip_entrypoint_processing=True,
        )


class ToolFactoryBySource(IToolFactory):
    """Manda as tools HTTP para ``http`` e as MCP (``mcp_tools``) para ``mcp``."""

    def __init__(self, *, http: IToolFactory, mcp: IToolFactory) -> None:
        self._http = http
        self._mcp = mcp

    async def create_tools_from_configs(self, tools: list[Tool]) -> list[Any]:
        """Tools HTTP primeiro, depois as MCP (o agente pede uma tool por vez)."""
        http = [tool for tool in tools if not tool.is_mcp]
        mcp = [tool for tool in tools if tool.is_mcp]
        created: list[Any] = []
        if http:
            created.extend(await self._http.create_tools_from_configs(http))
        if mcp:
            created.extend(await self._mcp.create_tools_from_configs(mcp))
        return created
