"""F4-03: cada chamada de tool (HTTP e MCP) gera um evento de auditoria estruturado.

O evento diz quem chamou (agente/time, usuário, run, sessão), qual tool, de que fonte, o resultado
(``ok``/``error``/``refused``/``cancelled``) e a duração; dos argumentos, só os nomes declarados no schema.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from agno.run.base import RunContext
from agno.tools.function import Function
from mcp.types import CallToolResult, TextContent

from src.domain.entities.tool import HttpMethod, ParameterType, Tool, ToolParameter
from src.infrastructure.mcp_client.sessions import McpSessions, schema_hash
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from src.infrastructure.runtime.agno.mcp_tool_factory import McpToolFactory
from src.infrastructure.runtime.agno.tool_audit import AUDIT_MESSAGE, audited_entrypoint
from src.infrastructure.security.ssrf import BlockedDestinationError
from tests.fakes.logger import LoggedRecord, RecordingLogger
from tests.fakes.mcp_server import FAKE_MCP_URL, FakeMcpServer, mcp_tool

SEARCH = mcp_tool("search", "Busca issues", {"q": {"type": "string"}, "limit": {"type": "integer"}})


def _audit(logger: RecordingLogger) -> list[LoggedRecord]:
    return [r for r in logger.records if r.context.get("audit") == "tool_call"]


def _http_tool(route: str = "https://api.example.com/cep") -> Tool:
    return Tool(
        id="cep",
        name="cep",
        description="Busca CEP",
        route=route,
        http_method=HttpMethod.GET,
        parameters=[
            ToolParameter(name="cep", type=ParameterType.STRING, description="CEP", required=True),
            ToolParameter(name="formato", type=ParameterType.STRING, description="formato", required=False),
        ],
    )


def _mcp_tool(allowlist: dict[str, str] | None = None) -> Tool:
    return Tool(
        id="github",
        name="GitHub",
        description="Servidor MCP",
        route=FAKE_MCP_URL,
        http_method=HttpMethod.GET,
        parameters=[],
        mcp_tools=allowlist or {"search": schema_hash(SEARCH.name, SEARCH.description, SEARCH.inputSchema)},
    )


async def _http_function(handler: Any, logger: RecordingLogger) -> tuple[HttpToolFactory, Function]:
    factory = HttpToolFactory(logger, transport=httpx.MockTransport(handler), retries=0)
    [function] = await factory.create_tools_from_configs([_http_tool()])
    return factory, function


async def _invoke(function: Function, **kwargs: Any) -> Any:
    assert function.entrypoint is not None
    return await function.entrypoint(**kwargs)


# ── HTTP ────────────────────────────────────────────────────────────


async def test_chamada_http_ok_gera_um_evento_com_tool_fonte_resultado_duracao_e_nomes_dos_argumentos():
    logger = RecordingLogger()
    factory, function = await _http_function(lambda req: httpx.Response(200, json={"ok": 1}), logger)
    try:
        result = await _invoke(function, cep="01001000")
    finally:
        await factory.aclose()

    assert result == "{'ok': 1}"
    [event] = _audit(logger)
    assert event.level == "info"
    assert event.message == AUDIT_MESSAGE
    context = dict(event.context)
    duration = context.pop("duration_ms")
    assert isinstance(duration, float) and duration >= 0
    assert context == {
        "audit": "tool_call",
        "source": "http",
        "tool_id": "cep",
        "function": "cep",
        "mcp_tool": None,
        "result": "ok",
        "reason": None,
        "error_type": None,
        "http_status": 200,
        "arguments": ["cep"],
        "undeclared_arguments": 0,
        "agent_id": None,
        "team_id": None,
        "user_id": None,
        "run_id": None,
        "session_id": None,
    }


async def test_identidade_do_run_vem_do_contexto_que_o_agno_injeta():
    logger = RecordingLogger()
    factory, function = await _http_function(lambda req: httpx.Response(200, json={}), logger)
    run = RunContext(run_id="run-1", session_id="sess-1", user_id="ana")
    try:
        await _invoke(
            function,
            cep="1",
            _agno_run_context=run,
            _agno_agent=SimpleNamespace(id="dev"),
            _agno_team=SimpleNamespace(id="suporte"),
        )
    finally:
        await factory.aclose()

    [event] = _audit(logger)
    assert {k: event.context[k] for k in ("agent_id", "team_id", "user_id", "run_id", "session_id")} == {
        "agent_id": "dev",
        "team_id": "suporte",
        "user_id": "ana",
        "run_id": "run-1",
        "session_id": "sess-1",
    }


async def test_argumento_invalido_e_recusado_sem_chamar_o_upstream():
    logger = RecordingLogger()
    seen: list[httpx.Request] = []
    factory, function = await _http_function(lambda req: seen.append(req) or httpx.Response(200), logger)
    try:
        await _invoke(function, cep="1", inventado="x", outro="y")
    finally:
        await factory.aclose()

    assert seen == []
    [event] = _audit(logger)
    assert event.level == "warning"
    assert (event.context["result"], event.context["reason"]) == ("refused", "invalid_arguments")
    assert (event.context["arguments"], event.context["undeclared_arguments"]) == (["cep"], 2)


async def test_destino_recusado_pela_guarda_e_recusa():
    def blocked(request: httpx.Request) -> httpx.Response:
        raise BlockedDestinationError("destino recusado")

    logger = RecordingLogger()
    factory, function = await _http_function(blocked, logger)
    try:
        await _invoke(function, cep="1")
    finally:
        await factory.aclose()

    [event] = _audit(logger)
    assert (event.context["result"], event.context["reason"]) == ("refused", "blocked_destination")
    assert event.context["error_type"] == "BlockedDestinationError"


async def test_status_de_erro_do_upstream_e_erro_com_o_status():
    logger = RecordingLogger()
    factory, function = await _http_function(lambda req: httpx.Response(503, text="fora"), logger)
    try:
        await _invoke(function, cep="1")
    finally:
        await factory.aclose()

    [event] = _audit(logger)
    assert event.level == "warning"
    assert {k: event.context[k] for k in ("result", "reason", "http_status")} == {
        "result": "error",
        "reason": "http_status",
        "http_status": 503,
    }


@pytest.mark.parametrize(
    ("exc", "reason"),
    [
        (httpx.ConnectError("x"), "request_failed"),
        (httpx.ReadTimeout("x"), "timeout"),
        (RuntimeError("x"), "unexpected"),
    ],
)
async def test_falha_de_rede_ou_inesperada_e_erro_so_com_o_tipo(exc: Exception, reason: str):
    def failing(request: httpx.Request) -> httpx.Response:
        raise exc

    logger = RecordingLogger()
    factory, function = await _http_function(failing, logger)
    try:
        await _invoke(function, cep="1")
    finally:
        await factory.aclose()

    [event] = _audit(logger)
    assert {k: event.context[k] for k in ("result", "reason", "error_type", "http_status")} == {
        "result": "error",
        "reason": reason,
        "error_type": type(exc).__name__,
        "http_status": None,
    }


async def test_chamada_cancelada_gera_evento_cancelled_e_propaga_o_cancelamento():
    started = asyncio.Event()

    async def hanging(request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.sleep(3600)
        raise AssertionError("não chega aqui")

    logger = RecordingLogger()
    factory = HttpToolFactory(logger, transport=httpx.MockTransport(hanging), retries=0)
    [function] = await factory.create_tools_from_configs([_http_tool()])
    try:
        task = asyncio.create_task(_invoke(function, cep="1"))
        await started.wait()
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        await factory.aclose()

    [event] = _audit(logger)
    assert (event.context["result"], event.context["reason"]) == ("cancelled", None)


# ── MCP ─────────────────────────────────────────────────────────────


async def _mcp_call(server: FakeMcpServer, logger: RecordingLogger, tool: Tool, **kwargs: Any) -> str:
    sessions = McpSessions(logger, transport_factory=server.transport)
    try:
        [function] = await McpToolFactory(sessions, logger).create_tools_from_configs([tool])
        return await _invoke(function, **kwargs)
    finally:
        await sessions.aclose()


async def test_chamada_mcp_ok_gera_evento_com_a_tool_do_servidor():
    logger = RecordingLogger()
    async with FakeMcpServer([SEARCH]) as server:
        server.results["search"] = "2 issues"
        result = await _mcp_call(server, logger, _mcp_tool(), q="bug", _agno_agent=SimpleNamespace(id="dev"))

    assert result == "2 issues"
    [event] = _audit(logger)
    assert event.level == "info"
    assert {k: event.context[k] for k in ("source", "tool_id", "function", "mcp_tool", "result", "agent_id")} == {
        "source": "mcp",
        "tool_id": "github",
        "function": "github__search",
        "mcp_tool": "search",
        "result": "ok",
        "agent_id": "dev",
    }
    assert (event.context["arguments"], event.context["http_status"]) == (["q"], None)


async def test_is_error_do_servidor_mcp_e_erro():
    logger = RecordingLogger()
    async with FakeMcpServer([SEARCH]) as server:
        server.results["search"] = CallToolResult(content=[TextContent(type="text", text="falhou")], isError=True)
        await _mcp_call(server, logger, _mcp_tool(), q="bug")

    [event] = _audit(logger)
    assert (event.context["result"], event.context["reason"]) == ("error", "tool_error")


async def test_schema_que_diverge_do_hash_fixado_e_recusa():
    logger = RecordingLogger()
    async with FakeMcpServer([SEARCH]) as server:
        sessions = McpSessions(logger, transport_factory=server.transport)
        try:
            outcome = await sessions.call_with_outcome(_mcp_tool({"search": "0" * 64}), "search", {})
        finally:
            await sessions.aclose()

    assert server.calls == []
    assert (outcome.result, outcome.reason) == ("refused", "schema_mismatch")


async def test_tool_fora_da_allowlist_e_recusa():
    logger = RecordingLogger()
    async with FakeMcpServer([SEARCH]) as server:
        sessions = McpSessions(logger, transport_factory=server.transport)
        try:
            outcome = await sessions.call_with_outcome(_mcp_tool(), "delete_repo", {})
        finally:
            await sessions.aclose()

    assert (outcome.result, outcome.reason) == ("refused", "not_allowlisted")
    assert outcome.text == "Erro na tool MCP github/delete_repo: tool fora da allowlist"


async def test_excecao_que_escapa_da_fonte_gera_evento_so_com_o_tipo_e_propaga():
    async def broken(kwargs: dict[str, Any]) -> Any:
        raise KeyError("texto-da-excecao-f403")

    logger = RecordingLogger()
    entrypoint = audited_entrypoint(
        broken, logger=logger, source="http", tool_id="t", function_name="t", declared_arguments=["a"]
    )
    with pytest.raises(KeyError):
        await entrypoint(a=1, b=2)

    [event] = _audit(logger)
    assert {k: event.context[k] for k in ("result", "reason", "error_type", "arguments", "undeclared_arguments")} == {
        "result": "error",
        "reason": "unexpected",
        "error_type": "KeyError",
        "arguments": ["a"],
        "undeclared_arguments": 1,
    }
    assert "texto-da-excecao-f403" not in repr(event)


class _BlockedTransport(httpx.AsyncBaseTransport):
    """Transporte cuja guarda recusa o destino na conexão (como o ``GuardedAsyncTransport``)."""

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        raise BlockedDestinationError("destino recusado")


async def test_guarda_ssrf_no_servidor_mcp_e_recusa_como_na_tool_http():
    sessions = McpSessions(RecordingLogger(), transport_factory=_BlockedTransport)
    try:
        outcome = await sessions.call_with_outcome(_mcp_tool(), "search", {})
    finally:
        await sessions.aclose()

    assert (outcome.result, outcome.reason, outcome.error_type) == (
        "refused",
        "blocked_destination",
        "BlockedDestinationError",
    )
    assert outcome.text == "Erro na tool MCP github/search: destino do servidor MCP recusado pela guarda SSRF"


async def test_route_mcp_nao_http_e_recusa():
    tool = _mcp_tool()
    tool.route = "stdio://python servidor.py"
    sessions = McpSessions(RecordingLogger(), transport_factory=_BlockedTransport)
    try:
        outcome = await sessions.call_with_outcome(tool, "search", {})
    finally:
        await sessions.aclose()

    assert (outcome.result, outcome.reason) == ("refused", "unsupported_route")


async def test_servidor_mcp_que_nao_responde_e_erro_e_nao_recusa():
    class _Down(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("x")

    sessions = McpSessions(RecordingLogger(), transport_factory=_Down)
    try:
        outcome = await sessions.call_with_outcome(_mcp_tool(), "search", {})
    finally:
        await sessions.aclose()

    assert (outcome.result, outcome.reason) == ("error", "server_unavailable")
