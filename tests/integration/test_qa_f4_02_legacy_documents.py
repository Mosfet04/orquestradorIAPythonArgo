"""QA do F4-02: documentos de tool do Mongo (e do YAML) antigos ou malformados pelo repositório real.

Repositório Mongo real sobre ``FakeMongoCollection``; o que importa é o comportamento observável: documento de antes
(sem ``mcp_tools``) segue tool HTTP, ``mcp_tools`` nulo também, e documento MCP malformado isola só ele, com log.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.infrastructure.mcp_client.sessions import schema_hash
from tests.fakes.config_stores import MongoBackend
from tests.fakes.logger import RecordingLogger

HASH = schema_hash("search", "Busca", {"type": "object"})


def _doc(tool_id: str, **extra: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "id": tool_id,
        "name": tool_id,
        "description": "d",
        "route": "https://api.example.com/x",
        "http_method": "GET",
        "parameters": [],
        "active": True,
    }
    doc.update(extra)
    return doc


@pytest.fixture
def backend(monkeypatch: pytest.MonkeyPatch) -> MongoBackend:
    return MongoBackend(monkeypatch)


async def test_documento_de_antes_e_mcp_tools_nulo_seguem_tool_http(backend: MongoBackend):
    repository = backend.tools([_doc("antigo"), _doc("nulo", mcp_tools=None)])

    tools = await repository.get_all_active_tools()

    assert [(t.id, t.mcp_tools, t.is_mcp) for t in tools] == [("antigo", None, False), ("nulo", None, False)]  # type: ignore[attr-defined]


async def test_documento_mcp_valido_e_lido_com_a_allowlist(backend: MongoBackend):
    repository = backend.tools([_doc("gh", mcp_tools={"search": HASH}, headers={"Authorization": "env:GH_API_KEY"})])

    [tool] = await repository.get_tools_by_ids(["gh"])

    assert tool.is_mcp and tool.mcp_tools == {"search": HASH}
    assert tool.headers == {"Authorization": "env:GH_API_KEY"}


@pytest.mark.parametrize(
    "bad",
    [
        {"mcp_tools": {}},
        {"mcp_tools": "search"},
        {"mcp_tools": ["search"]},
        {"mcp_tools": {"search": "xyz"}},
        {"mcp_tools": {"search": HASH.upper()}},
        {"mcp_tools": {"a.b": HASH}},
        {"mcp_tools": {"search": HASH}, "parameters": [{"name": "q", "type": "string", "description": "x"}]},
    ],
)
async def test_documento_mcp_malformado_isola_so_ele_e_loga(backend: MongoBackend, bad: dict[str, Any]):
    logger = RecordingLogger()
    repository = backend.tools([_doc("ruim", **bad), _doc("bom")], logger)

    tools = await repository.get_all_active_tools()

    assert [t.id for t in tools] == ["bom"]
    assert any(r.level == "error" and r.message == "Documento de tool inválido ignorado" for r in logger.records)
    assert HASH not in repr(logger.records)
