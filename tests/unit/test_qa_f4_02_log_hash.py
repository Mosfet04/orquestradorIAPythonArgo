"""QA do F4-02: o hash divergente chega inteiro ao log de produção (é como o operador fixa o hash novo).

O item diz que o log da recusa traz ``expected_hash`` e ``observed_hash`` (o roadmap cita o ``observed_hash`` do log
como a forma de obter o hash). O logger de produção (``StructlogLoggerAdapter``) tem um padrão de sanitização para
"AWS secret key" (40 caracteres alfanuméricos) que mascara os 40 primeiros caracteres de um SHA-256 hexadecimal.
"""

from __future__ import annotations

import logging

import pytest

from src.domain.entities.tool import HttpMethod, Tool
from src.infrastructure.logging import setup_structlog
from src.infrastructure.logging.logger_adapter import StructlogLoggerAdapter
from src.infrastructure.mcp_client.sessions import McpSessions, schema_hash
from tests.fakes.mcp_server import FAKE_MCP_URL, FakeMcpServer, mcp_tool

SEARCH = mcp_tool("search", "Busca issues")
CHANGED = mcp_tool("search", "Busca issues, versão nova")


async def test_hash_observado_e_esperado_aparecem_inteiros_no_log_de_producao(caplog: pytest.LogCaptureFixture, capsys):
    setup_structlog()
    caplog.set_level(logging.DEBUG)
    expected = schema_hash(SEARCH.name, SEARCH.description, SEARCH.inputSchema)
    observed = schema_hash(CHANGED.name, CHANGED.description, CHANGED.inputSchema)
    tool = Tool(
        id="github", name="GitHub", description="d", route=FAKE_MCP_URL, http_method=HttpMethod.GET,
        parameters=[], mcp_tools={"search": expected},
    )
    async with FakeMcpServer([CHANGED]) as server:
        sessions = McpSessions(StructlogLoggerAdapter("app"), transport_factory=server.transport)
        try:
            await sessions.pinned_tools(tool)
        finally:
            await sessions.aclose()

    output = caplog.text + capsys.readouterr().out
    assert observed in output
    assert expected in output
