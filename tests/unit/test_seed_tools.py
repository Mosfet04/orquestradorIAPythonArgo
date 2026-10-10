"""Seed do Mongo (F1-05): as tools de ``mongo-init/init-db.js`` passam pelo mapper real.

Sem Node: o bloco ``db.tools.insertMany([...])`` do seed é JSON puro (sem comentário,
``new Date()`` ou vírgula sobrando) e é lido com ``json``. Se o bloco deixar de ser JSON,
este teste quebra: mantenha-o assim.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

import pytest

from src.domain.entities.tool import Tool
from src.infrastructure.http.http_tool_factory import HttpToolFactory
from src.infrastructure.repositories.config_documents import map_tool_document
from tests.fakes import RecordingLogger

SEED = Path(__file__).resolve().parents[2] / "mongo-init" / "init-db.js"
_SECRET_NAME = re.compile(r"key|token|secret|senha|password|appid|auth", re.IGNORECASE)


def _insert_many_block(collection: str) -> str:
    source = SEED.read_text(encoding="utf-8")
    start = source.index(f"db.{collection}.insertMany(") + len(f"db.{collection}.insertMany(")
    end = source.index("]);", start) + 1
    return source[start:end]


def _seed_tool_docs() -> list[dict[str, Any]]:
    docs = json.loads(_insert_many_block("tools"))
    assert isinstance(docs, list)
    return docs


def _seed_agent_tool_ids() -> set[str]:
    block = _insert_many_block("agents_config")
    ids: set[str] = set()
    for raw in re.findall(r'"tools_ids":\s*(\[[^\]]*\])', block):
        ids.update(json.loads(raw))
    return ids


def test_seed_tem_tools():
    assert [d["id"] for d in _seed_tool_docs()] == ["weather-tool", "calculator-tool"]


def test_todas_as_tools_do_seed_passam_pelo_mapper_do_repositorio_sem_perder_campos():
    docs = _seed_tool_docs()

    tools = [map_tool_document(doc) for doc in docs]

    assert [t.id for t in tools] == [d["id"] for d in docs]
    for doc, tool in zip(docs, tools, strict=True):
        assert tool.active is True, f"{tool.id}: o repositório só busca tools com active=true"
        assert tool.route.startswith("https://"), tool.id
        assert tool.http_method.value == doc["http_method"]
        assert tool.instructions, f"{tool.id}: sem instruções para o system message"
        assert [(p.name, p.type.value, p.required) for p in tool.parameters] == [
            (p["name"], p["type"], p["required"]) for p in doc["parameters"]
        ]


def test_seed_nao_usa_o_formato_legado_que_o_repositorio_ignora():
    for doc in _seed_tool_docs():
        assert "http_config" not in doc, doc["id"]
        assert {"id", "name", "description", "route", "http_method", "parameters", "active"} <= doc.keys()


def test_seed_nao_pede_segredo_ao_llm_nem_guarda_segredo_no_documento():
    for doc in _seed_tool_docs():
        names = [p["name"] for p in doc["parameters"]]
        assert not [n for n in names if _SECRET_NAME.search(n)], doc["id"]
        assert not [h for h in doc.get("headers", {}) if _SECRET_NAME.search(h)], doc["id"]


def test_tools_referenciadas_pelos_agentes_do_seed_existem():
    seeded = {d["id"] for d in _seed_tool_docs()}

    assert _seed_agent_tool_ids() <= seeded


@pytest.mark.parametrize("doc", _seed_tool_docs(), ids=lambda d: d["id"])
async def test_tool_do_seed_vira_function_com_schema(doc: dict[str, Any]):
    logger = RecordingLogger()
    tool: Tool = map_tool_document(doc)

    (function,) = await HttpToolFactory(logger=logger).create_tools_from_configs([tool])

    assert logger.messages("error") == []
    assert function.name == doc["id"]
    assert sorted(function.parameters["properties"]) == sorted(p["name"] for p in doc["parameters"])
    assert function.parameters["required"] == [p["name"] for p in doc["parameters"] if p["required"]]
