"""F4-02: ``Tool.mcp_tools`` (allowlist de tools de um servidor MCP com o hash do schema fixado)."""

from __future__ import annotations

import pytest

from src.domain.entities.tool import HttpMethod, ParameterType, Tool, ToolParameter
from src.domain.entities.validation import InvalidField

HASH = "a" * 64


def _tool(**overrides: object) -> Tool:
    fields: dict[str, object] = {
        "id": "github",
        "name": "GitHub",
        "description": "Servidor MCP do GitHub",
        "route": "https://mcp.example.com/mcp",
        "http_method": HttpMethod.GET,
        "parameters": [],
        "mcp_tools": {"search_issues": HASH},
    }
    fields.update(overrides)
    return Tool(**fields)  # type: ignore[arg-type]


def test_tool_sem_mcp_tools_segue_http():
    tool = _tool(mcp_tools=None)
    assert tool.mcp_tools is None
    assert tool.is_mcp is False


def test_tool_mcp_guarda_a_allowlist():
    tool = _tool(mcp_tools={"search_issues": HASH, "get-issue": "b" * 64})
    assert tool.is_mcp is True
    assert tool.mcp_tools == {"search_issues": HASH, "get-issue": "b" * 64}


@pytest.mark.parametrize(
    "mcp_tools",
    [
        {},
        {"": HASH},
        {"nome com espaço": HASH},
        {"x" * 65: HASH},
        {"ok": "A" * 64},
        {"ok": "a" * 63},
        {"ok": "sha256:" + HASH},
        {"ok": 1},
        ["ok"],
    ],
)
def test_allowlist_invalida_e_recusada(mcp_tools):
    with pytest.raises(InvalidField) as exc:
        _tool(mcp_tools=mcp_tools)
    assert exc.value.field == "mcp_tools"


def test_tool_mcp_nao_declara_parameters():
    parameter = ToolParameter(name="q", type=ParameterType.STRING, description="busca")
    with pytest.raises(InvalidField) as exc:
        _tool(parameters=[parameter])
    assert exc.value.field == "parameters"
