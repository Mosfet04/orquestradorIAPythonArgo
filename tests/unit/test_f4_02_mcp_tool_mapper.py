"""F4-02: ``mcp_tools`` no documento de tool (Mongo e YAML usam o mesmo mapper)."""

from __future__ import annotations

import pytest

from src.domain.entities.validation import InvalidField
from src.infrastructure.repositories.config_documents import entity_document, map_tool_document

HASH = "0" * 64


def _doc(**extra: object) -> dict[str, object]:
    return {
        "id": "github",
        "name": "GitHub",
        "description": "Servidor MCP",
        "route": "https://mcp.example.com/mcp",
        **extra,
    }


def test_documento_legado_sem_mcp_tools_segue_tool_http():
    assert map_tool_document(_doc(http_method="GET")).mcp_tools is None


@pytest.mark.parametrize("strict", [False, True])
def test_mcp_tools_lido_nos_dois_modos(strict):
    tool = map_tool_document(_doc(mcp_tools={"search": HASH}), strict=strict)
    assert tool.is_mcp is True
    assert tool.mcp_tools == {"search": HASH}


@pytest.mark.parametrize("raw", ["search", ["search"], {"search": 1}, {1: HASH}])
def test_mcp_tools_com_tipo_errado_invalida_o_documento(raw):
    with pytest.raises(InvalidField) as exc:
        map_tool_document(_doc(mcp_tools=raw))
    assert exc.value.field == "mcp_tools"


def test_ida_e_volta_pelo_documento_estrito():
    tool = map_tool_document(_doc(mcp_tools={"search": HASH}, headers={"Authorization": "env:GITHUB_API_KEY"}))
    assert map_tool_document(entity_document(tool), strict=True) == tool
