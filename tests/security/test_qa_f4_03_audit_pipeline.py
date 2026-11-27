"""QA F4-03: o evento de auditoria pelo pipeline real de logging de produção (``StructlogLogger`` + sanitizador + JSON).

O dev testou o sanitizador com um dict montado à mão; aqui o evento sai do envoltório real das duas fábricas, passa
pelo ``setup_structlog`` em modo produção e é lido como o coletor de logs o leria.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from types import SimpleNamespace
from typing import Any

import httpx
import pytest
import structlog
from agno.run.base import RunContext

from src.domain.entities.tool import HttpMethod, ParameterType, Tool, ToolParameter
from src.infrastructure.logging.structlog_logger import StructlogLogger, setup_structlog
from src.infrastructure.mcp_client.sessions import McpSessions, schema_hash
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from src.infrastructure.runtime.agno.mcp_tool_factory import McpToolFactory
from tests.fakes.mcp_server import FAKE_MCP_URL, FakeMcpServer, mcp_tool

ARGUMENT_VALUE = "123.456.789-09"
SESSION = "sessao-secreta-qa-f403"
RUN_ID = "a1b2c3d4-e5f6-4a7b-8c9d-0e1f2a3b4c5d"
SEARCH = mcp_tool("search", "Busca", {"q": {"type": "string"}})
EXPECTED_KEYS = {
    "audit", "source", "tool_id", "function", "mcp_tool", "result", "reason", "error_type", "http_status",
    "duration_ms", "arguments", "undeclared_arguments", "agent_id", "team_id", "user_id", "run_id", "session_id",
}  # fmt: skip


@pytest.fixture
def production_logger(monkeypatch, tmp_path, caplog) -> Iterator[StructlogLogger]:
    """Logger de produção (JSON) com a config global do structlog e o nível do ``httpx`` restaurados no fim."""
    saved = structlog.get_config()
    httpx_logger = logging.getLogger("httpx")
    monkeypatch.setattr(httpx_logger, "level", httpx_logger.level)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.chdir(tmp_path)
    setup_structlog()
    caplog.set_level(logging.INFO)
    try:
        yield StructlogLogger("qa_f4_03_audit")
    finally:
        structlog.configure(**saved)


def _emitted(caplog) -> list[dict[str, Any]]:
    parsed = [json.loads(r.getMessage()) for r in caplog.records if r.getMessage().startswith("{")]
    return [e for e in parsed if e.get("audit") == "tool_call"]


def _http_tool(tool_id: str = "cep") -> Tool:
    return Tool(
        id=tool_id,
        name="cep",
        description="Busca CEP",
        route="https://api.example.com/cep",
        http_method=HttpMethod.GET,
        parameters=[ToolParameter(name="cpf", type=ParameterType.STRING, description="CPF", required=True)],
    )


def _run() -> dict[str, Any]:
    return {
        "_agno_run_context": RunContext(run_id=RUN_ID, session_id=SESSION, user_id="ana"),
        "_agno_agent": SimpleNamespace(id="dev"),
    }


@pytest.mark.parametrize("status", [200, 503])
async def test_evento_http_em_producao_e_json_completo_com_so_a_sessao_mascarada(production_logger, caplog, status):
    factory = HttpToolFactory(
        production_logger,  # type: ignore[arg-type]
        transport=httpx.MockTransport(lambda req: httpx.Response(status, text="corpo")),
        retries=0,
    )
    [function] = await factory.create_tools_from_configs([_http_tool()])
    assert function.entrypoint is not None
    try:
        await function.entrypoint(cpf=ARGUMENT_VALUE, **_run())
    finally:
        await factory.aclose()

    [event] = _emitted(caplog)
    assert EXPECTED_KEYS <= set(event)
    assert event["session_id"].startswith("***MASKED***[hash:") and SESSION not in json.dumps(event)
    assert ARGUMENT_VALUE not in json.dumps(event)
    assert (event["source"], event["tool_id"], event["function"], event["arguments"]) == (
        "http", "cep", "cep", ["cpf"],
    )  # fmt: skip
    assert (event["agent_id"], event["user_id"], event["run_id"]) == ("dev", "ana", RUN_ID)
    assert event["result"] == ("ok" if status == 200 else "error")
    assert event["http_status"] == status
    assert isinstance(event["duration_ms"], float) and event["undeclared_arguments"] == 0
    assert event["log_level"] == ("info" if status == 200 else "warning")


async def test_evento_mcp_em_producao_preserva_function_mcp_tool_e_tipos(production_logger, caplog):
    tool = Tool(
        id="github", name="GitHub", description="MCP", route=FAKE_MCP_URL, http_method=HttpMethod.GET, parameters=[],
        mcp_tools={"search": schema_hash(SEARCH.name, SEARCH.description, SEARCH.inputSchema)},
    )  # fmt: skip
    async with FakeMcpServer([SEARCH]) as server:
        sessions = McpSessions(production_logger, transport_factory=server.transport)  # type: ignore[arg-type]
        try:
            [function] = await McpToolFactory(sessions, production_logger).create_tools_from_configs([tool])  # type: ignore[arg-type]
            assert function.entrypoint is not None
            await function.entrypoint(q=ARGUMENT_VALUE, **_run())
        finally:
            await sessions.aclose()

    [event] = _emitted(caplog)
    assert EXPECTED_KEYS <= set(event)
    assert (event["source"], event["function"], event["mcp_tool"], event["arguments"]) == (
        "mcp", "github__search", "search", ["q"],
    )  # fmt: skip
    assert event["http_status"] is None and event["result"] == "ok"
    assert ARGUMENT_VALUE not in json.dumps(event)


async def test_nome_de_tool_longo_sai_inteiro_no_evento(production_logger, caplog):
    """BUG-F4-03-A: ``tool_id``/``function`` aceitam até 64 caracteres ``[A-Za-z0-9_-]``; um nome só alfanumérico
    de 40+ virava ``=***AWS_SECRET_KEY_MASKED***`` e o evento deixava de dizer qual tool foi chamada."""
    long_id = "consultaCadastroClientesPessoaFisicaV2Interno"  # 44 caracteres alfanuméricos
    factory = HttpToolFactory(
        production_logger,  # type: ignore[arg-type]
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={})),
        retries=0,
    )
    [function] = await factory.create_tools_from_configs([_http_tool(long_id)])
    assert function.entrypoint is not None
    try:
        await function.entrypoint(cpf="1")
    finally:
        await factory.aclose()

    [event] = _emitted(caplog)
    assert (event["tool_id"], event["function"]) == (long_id, long_id)


async def test_run_id_uuid_so_com_digitos_sai_inteiro_no_evento(production_logger, caplog):
    """BUG-F4-03-B: um uuid4 como o do agno com 16 dígitos seguidos (grupos 4 e 5, ou 1 a 3) virava
    ``=***CREDIT_CARD_MASKED***`` e o evento perdia a correlação com o run."""
    run_id = "12345678-1234-4234-8234-123456789012"
    factory = HttpToolFactory(
        production_logger,  # type: ignore[arg-type]
        transport=httpx.MockTransport(lambda req: httpx.Response(200, json={})),
        retries=0,
    )
    [function] = await factory.create_tools_from_configs([_http_tool()])
    assert function.entrypoint is not None
    try:
        await function.entrypoint(cpf="1", _agno_run_context=RunContext(run_id=run_id, session_id="s", user_id="ana"))
    finally:
        await factory.aclose()

    [event] = _emitted(caplog)
    assert event["run_id"] == run_id
