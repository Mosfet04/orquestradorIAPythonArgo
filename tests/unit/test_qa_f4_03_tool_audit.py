"""QA F4-03: lacunas dos testes do dev no log de auditoria de tool call (HTTP e MCP).

Cobre: todo ``reason`` do vocabulário fixo (alcançado pelo caminho real, com o evento pelo entrypoint da ``Function``),
cancelamento e exceção na fonte MCP, evento único com retry, identidade sem vazamento entre chamadas concorrentes,
schema MCP sem ``properties`` e argumentos hostis (nome não declarado, volume).
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
from agno.run.base import RunContext
from agno.tools.function import Function
from mcp import types
from mcp.types import CallToolResult

from src.domain.entities.tool import HttpMethod, ParameterType, Tool, ToolParameter
from src.infrastructure.mcp_client.sessions import McpSessions, schema_hash
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from src.infrastructure.runtime.agno.mcp_tool_factory import McpToolFactory
from tests.fakes.logger import LoggedRecord, RecordingLogger
from tests.fakes.mcp_server import FAKE_MCP_URL, FakeMcpServer, mcp_tool

SEARCH = mcp_tool("search", "Busca issues", {"q": {"type": "string"}})
FIELDS = ("result", "reason", "error_type", "http_status")


def _audit(logger: RecordingLogger) -> list[LoggedRecord]:
    return [r for r in logger.records if r.context.get("audit") == "tool_call"]


def _fields(event: LoggedRecord) -> dict[str, Any]:
    return {k: event.context[k] for k in FIELDS}


def _http_tool(headers: dict[str, str] | None = None, method: HttpMethod = HttpMethod.GET) -> Tool:
    return Tool(
        id="cep",
        name="cep",
        description="Busca CEP",
        route="https://api.example.com/cep",
        http_method=method,
        parameters=[
            ToolParameter(name="cep", type=ParameterType.STRING, description="CEP", required=True),
            ToolParameter(name="formato", type=ParameterType.STRING, description="formato", required=False),
        ],
        headers=headers,
    )


def _mcp_tool(route: str = FAKE_MCP_URL, tools: list[types.Tool] | None = None) -> Tool:
    return Tool(
        id="github",
        name="GitHub",
        description="Servidor MCP",
        route=route,
        http_method=HttpMethod.GET,
        parameters=[],
        mcp_tools={t.name: schema_hash(t.name, t.description, t.inputSchema) for t in (tools or [SEARCH])},
    )


async def _http_function(handler: Any, logger: RecordingLogger, **kwargs: Any) -> tuple[HttpToolFactory, Function]:
    tool = kwargs.pop("tool", None) or _http_tool()
    factory = HttpToolFactory(logger, transport=httpx.MockTransport(handler), **{"retries": 0, **kwargs})
    [function] = await factory.create_tools_from_configs([tool])
    return factory, function


async def _invoke(function: Function, **kwargs: Any) -> str:
    assert function.entrypoint is not None
    result = await function.entrypoint(**kwargs)
    assert isinstance(result, str)
    return result


# ── HTTP: vocabulário de reason ainda sem teste ─────────────────────


async def test_header_por_referencia_nao_resolvido_e_erro_e_a_chamada_nao_sai(monkeypatch):
    monkeypatch.delenv("CEP_AUSENTE_API_KEY", raising=False)
    seen: list[httpx.Request] = []
    logger = RecordingLogger()
    factory, function = await _http_function(
        lambda req: seen.append(req) or httpx.Response(200),
        logger,
        tool=_http_tool({"Authorization": "env:CEP_AUSENTE_API_KEY"}),
    )
    try:
        await _invoke(function, cep="1")
    finally:
        await factory.aclose()

    assert seen == []
    [event] = _audit(logger)
    assert event.level == "warning"
    assert _fields(event) == {
        "result": "error",
        "reason": "unresolved_header",
        "error_type": "SecretResolutionError",
        "http_status": None,
    }
    assert "CEP_AUSENTE_API_KEY" not in repr(event)


async def test_resposta_acima_do_limite_e_erro_response_too_large():
    logger = RecordingLogger()
    factory, function = await _http_function(
        lambda req: httpx.Response(200, content=b"x" * 500), logger, max_response_bytes=10
    )
    try:
        await _invoke(function, cep="1")
    finally:
        await factory.aclose()

    [event] = _audit(logger)
    assert _fields(event) == {
        "result": "error",
        "reason": "response_too_large",
        "error_type": None,
        "http_status": None,
    }


async def test_content_encoding_sem_suporte_e_erro_unsupported_encoding():
    logger = RecordingLogger()
    factory, function = await _http_function(
        lambda req: httpx.Response(200, headers={"Content-Encoding": "br"}, content=b"\x8b\x00"), logger
    )
    try:
        await _invoke(function, cep="1")
    finally:
        await factory.aclose()

    [event] = _audit(logger)
    assert _fields(event) == {
        "result": "error",
        "reason": "unsupported_encoding",
        "error_type": None,
        "http_status": None,
    }


async def test_cliente_http_fechado_e_erro_so_com_o_tipo_e_ainda_audita():
    """Depois do ``aclose`` (shutdown) a chamada não sai, mas continua gerando exatamente um evento."""
    logger = RecordingLogger()
    factory, function = await _http_function(lambda req: httpx.Response(200, json={}), logger)
    await factory.aclose()

    text = await _invoke(function, cep="1")

    [event] = _audit(logger)
    assert _fields(event) == {
        "result": "error",
        "reason": "unexpected",
        "error_type": "RuntimeError",
        "http_status": None,
    }
    assert event.level == "warning"
    assert text == "Erro inesperado ao chamar a tool (RuntimeError)"  # erro curto ao modelo, sem o texto da exceção


async def test_retry_gera_um_unico_evento_com_o_resultado_final():
    attempts: list[int] = []

    def flaky(request: httpx.Request) -> httpx.Response:
        attempts.append(1)
        if len(attempts) < 3:
            raise httpx.ConnectError("x")
        return httpx.Response(200, json={"ok": 1})

    logger = RecordingLogger()
    factory, function = await _http_function(flaky, logger, retries=2, retry_backoff=0.0)
    try:
        await _invoke(function, cep="1")
    finally:
        await factory.aclose()

    assert len(attempts) == 3
    [event] = _audit(logger)
    assert (_fields(event)["result"], _fields(event)["http_status"]) == ("ok", 200)


async def test_retry_esgotado_gera_um_unico_evento_de_erro():
    logger = RecordingLogger()

    def down(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("x")

    factory, function = await _http_function(down, logger, retries=2, retry_backoff=0.0)
    try:
        await _invoke(function, cep="1")
    finally:
        await factory.aclose()

    [event] = _audit(logger)
    assert _fields(event)["result"] == "error"


# ── HTTP: concorrência e identidade ─────────────────────────────────


async def test_chamadas_concorrentes_cada_evento_leva_a_identidade_e_os_argumentos_da_propria_chamada():
    async def handler(request: httpx.Request) -> httpx.Response:
        await asyncio.sleep(0.001 * (hash(request.url.query) % 5))
        return httpx.Response(200, json={})

    logger = RecordingLogger()
    factory = HttpToolFactory(logger, transport=httpx.MockTransport(handler), retries=0)
    [function] = await factory.create_tools_from_configs([_http_tool()])

    async def call(i: int) -> None:
        args: dict[str, Any] = {"cep": str(i)}
        if i % 2:
            args["formato"] = "json"
        if i % 3 == 0:
            args["extra"] = "x"
        await _invoke(
            function,
            **args,
            _agno_run_context=RunContext(run_id=f"run-{i}", session_id=f"s-{i}", user_id=f"u-{i}"),
            _agno_agent=SimpleNamespace(id=f"agent-{i % 4}"),
        )

    try:
        await asyncio.gather(*(call(i) for i in range(40)))
    finally:
        await factory.aclose()

    events = {e.context["run_id"]: e.context for e in _audit(logger)}
    assert len(events) == 40
    for i in range(40):
        context = events[f"run-{i}"]
        invalid = i % 3 == 0
        assert context["user_id"] == f"u-{i}"
        assert context["session_id"] == f"s-{i}"
        assert context["agent_id"] == f"agent-{i % 4}"
        assert context["result"] == ("refused" if invalid else "ok")
        assert context["arguments"] == (["cep", "formato"] if i % 2 else ["cep"])
        assert context["undeclared_arguments"] == (1 if invalid else 0)


async def test_chamada_sem_run_context_nao_herda_identidade_da_anterior():
    logger = RecordingLogger()
    factory, function = await _http_function(lambda req: httpx.Response(200, json={}), logger)
    try:
        await _invoke(
            function,
            cep="1",
            _agno_run_context=RunContext(run_id="r", session_id="s", user_id="ana"),
            _agno_agent=SimpleNamespace(id="dev"),
        )
        await _invoke(function, cep="1")
    finally:
        await factory.aclose()

    first, second = _audit(logger)
    assert first.context["user_id"] == "ana"
    assert {second.context[k] for k in ("agent_id", "team_id", "user_id", "run_id", "session_id")} == {None}


async def test_milhares_de_argumentos_nao_declarados_viram_so_a_contagem():
    logger = RecordingLogger()
    factory, function = await _http_function(lambda req: httpx.Response(200, json={}), logger)
    hostile = {f"campo-{i}\nforjado": "valor-hostil" for i in range(2000)}
    try:
        await _invoke(function, cep="1", **hostile)
    finally:
        await factory.aclose()

    [event] = _audit(logger)
    assert (event.context["arguments"], event.context["undeclared_arguments"]) == (["cep"], 2000)
    assert "campo-" not in repr(event) and "valor-hostil" not in repr(event)


# ── MCP: vocabulário de reason ainda sem teste ──────────────────────


async def _mcp(
    server: FakeMcpServer, logger: RecordingLogger, tool: Tool | None = None, **kwargs: Any
) -> tuple[McpSessions, Function]:
    sessions = McpSessions(logger, transport_factory=server.transport, **kwargs)
    [function] = await McpToolFactory(sessions, logger).create_tools_from_configs([tool or _mcp_tool()])
    return sessions, function


async def test_resultado_mcp_acima_do_limite_e_erro_result_too_large():
    logger = RecordingLogger()
    async with FakeMcpServer([SEARCH]) as server:
        sessions, function = await _mcp(server, logger, max_result_chars=10)
        try:
            server.results["search"] = "x" * 11
            text = await _invoke(function, q="a")
        finally:
            await sessions.aclose()

    assert "acima do limite" in text
    [event] = _audit(logger)
    assert event.level == "warning"
    assert _fields(event) == {"result": "error", "reason": "result_too_large", "error_type": None, "http_status": None}


async def test_falha_na_chamada_mcp_e_erro_call_failed_so_com_o_tipo():
    logger = RecordingLogger()
    async with FakeMcpServer([SEARCH]) as server:
        sessions, function = await _mcp(server, logger)
        try:
            server.fail_posts = 1
            await _invoke(function, q="a")
        finally:
            await sessions.aclose()

    [event] = _audit(logger)
    fields = _fields(event)
    assert (fields["result"], fields["reason"], fields["http_status"]) == ("error", "call_failed", None)
    assert isinstance(fields["error_type"], str) and fields["error_type"].isidentifier()
    assert "falha simulada" not in repr(event)


async def test_config_do_servidor_mcp_mudou_e_erro_config_changed():
    logger = RecordingLogger()
    async with FakeMcpServer([SEARCH], stateless=False) as server:
        sessions, function = await _mcp(server, logger)
        try:
            await sessions.pinned_tools(_mcp_tool("https://mcp.example.com/mcp?v=2"))
            await _invoke(function, q="a")
        finally:
            await sessions.aclose()

    assert server.calls == []
    [event] = _audit(logger)
    assert _fields(event) == {"result": "error", "reason": "config_changed", "error_type": None, "http_status": None}


async def test_cliente_mcp_fechado_e_erro_client_closed_e_ainda_audita():
    logger = RecordingLogger()
    async with FakeMcpServer([SEARCH]) as server:
        sessions, function = await _mcp(server, logger)
        await sessions.aclose()
        text = await _invoke(function, q="a")

    assert "já fechado" in text
    [event] = _audit(logger)
    assert _fields(event) == {"result": "error", "reason": "client_closed", "error_type": None, "http_status": None}


async def test_servidor_mcp_com_schema_alterado_pelo_entrypoint_gera_evento_refused():
    """Pelo caminho completo (``Function``), não só por ``call_with_outcome``: schema divergente = refused."""
    logger = RecordingLogger()
    async with FakeMcpServer([SEARCH]) as server:
        sessions, function = await _mcp(server, logger)
        try:
            server.tools = [mcp_tool("search", "Descrição trocada pelo servidor")]
            server.fail_posts = 1  # derruba a sessão: a reconexão relê o tools/list e confere o hash
            await _invoke(function, q="a")
            await _invoke(function, q="a")
        finally:
            await sessions.aclose()

    first, second = _audit(logger)
    assert first.context["result"] == "error"
    assert _fields(second)["reason"] == "schema_mismatch" and _fields(second)["result"] == "refused"
    assert server.calls == []


# ── MCP: cancelamento, exceção e argumentos ─────────────────────────


async def test_chamada_mcp_cancelada_gera_evento_cancelled_e_propaga():
    logger = RecordingLogger()
    async with FakeMcpServer([SEARCH]) as server:
        sessions, function = await _mcp(server, logger)
        server.delays["tools/call"] = 3600
        task = asyncio.create_task(_invoke(function, q="segredo-no-argumento"))
        try:
            for _ in range(500):
                if "tools/call" in server.methods():
                    break
                await asyncio.sleep(0.01)
            else:  # pragma: no cover - só se o fake não receber a chamada
                raise AssertionError("o servidor não recebeu a chamada")
            task.cancel()
            with pytest.raises(asyncio.CancelledError):
                await task
        finally:
            await sessions.aclose()

    [event] = _audit(logger)
    assert event.level == "warning"
    assert (event.context["result"], event.context["reason"], event.context["source"]) == ("cancelled", None, "mcp")
    assert event.context["mcp_tool"] == "search" and event.context["function"] == "github__search"
    assert "segredo-no-argumento" not in repr(event)


async def test_excecao_que_escapa_do_mcp_gera_evento_com_o_tipo_e_propaga(monkeypatch):
    logger = RecordingLogger()
    async with FakeMcpServer([SEARCH]) as server:
        sessions, function = await _mcp(server, logger)

        async def boom(*args: Any, **kwargs: Any) -> Any:
            raise RuntimeError("texto-da-excecao-qa-f403")

        monkeypatch.setattr(sessions, "call_with_outcome", boom)
        try:
            with pytest.raises(RuntimeError):
                await _invoke(function, q="a")
        finally:
            await sessions.aclose()

    [event] = _audit(logger)
    assert _fields(event) == {
        "result": "error",
        "reason": "unexpected",
        "error_type": "RuntimeError",
        "http_status": None,
    }
    assert "texto-da-excecao-qa-f403" not in repr(event)


async def test_schema_mcp_sem_properties_trata_todo_argumento_como_nao_declarado():
    bare = types.Tool(name="ping", description="Ping", inputSchema={"type": "object"})
    logger = RecordingLogger()
    async with FakeMcpServer([bare]) as server:
        sessions, function = await _mcp(server, logger, _mcp_tool(tools=[bare]))
        try:
            await _invoke(function, a="1", b="2")
        finally:
            await sessions.aclose()

    [event] = _audit(logger)
    assert (event.context["arguments"], event.context["undeclared_arguments"]) == ([], 2)
    assert event.context["result"] == "ok"


async def test_chamadas_mcp_concorrentes_cada_evento_leva_a_identidade_da_propria_chamada():
    logger = RecordingLogger()
    async with FakeMcpServer([SEARCH]) as server:
        sessions, function = await _mcp(server, logger)
        try:
            await asyncio.gather(
                *(
                    _invoke(
                        function,
                        q="x",
                        _agno_run_context=RunContext(run_id=f"run-{i}", session_id="s", user_id=f"u-{i}"),
                        _agno_agent=SimpleNamespace(id=f"a-{i}"),
                    )
                    for i in range(15)
                )
            )
        finally:
            await sessions.aclose()

    events = _audit(logger)
    assert len(events) == 15
    assert {(e.context["run_id"], e.context["user_id"], e.context["agent_id"]) for e in events} == {
        (f"run-{i}", f"u-{i}", f"a-{i}") for i in range(15)
    }
    assert {e.context["result"] for e in events} == {"ok"}


async def test_resultado_mcp_estruturado_continua_ok_e_nao_vaza_no_evento():
    logger = RecordingLogger()
    async with FakeMcpServer([SEARCH]) as server:
        sessions, function = await _mcp(server, logger)
        try:
            server.results["search"] = CallToolResult(content=[], structuredContent={"k": "estruturado-qa"})
            text = await _invoke(function, q="a")
        finally:
            await sessions.aclose()

    assert "estruturado-qa" in text
    [event] = _audit(logger)
    assert event.context["result"] == "ok"
    assert "estruturado-qa" not in repr(event)
