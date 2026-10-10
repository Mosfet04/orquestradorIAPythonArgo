"""QA do F2-06 (rodada 3): listas de ids sujas (``tools_ids``/``memberIds``) num YAML real, de ponta a ponta.

App real com ``CONFIG_STORE=yaml`` (mesma pilha de ``test_qa_f2_06_yaml_e2e.py``): o agente com
``tools_ids`` sujo sobe e é servido sem as entradas sujas, a tool válida da mesma lista executa, o team
com ``memberIds`` legado sujo sobe com os membros de texto, e o conteúdo sujo nunca vai para o log.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from agno.models.response import ModelResponse

from src.infrastructure import dependency_injection as di
from tests.fakes import RecordingLogger
from tests.integration.test_qa_f2_06_yaml_e2e import BASE, RUN, _World, yaml_app

MARKER = "SEGREDO-IDS-SUJOS-R3"
NOT_TEXT_ITEMS = "Itens que não são texto ignorados na lista de ids"
NOT_A_LIST = "Lista de ids que não é lista ignorada"

CONFIG = f"""
agents:
  - id: sujo
    nome: Agente de ids sujos
    factoryIaModel: ollama
    model: m-sujo
    descricao: d
    prompt: [Você é um agente de teste.]
    tools_ids: [cep, 7, {{x: {MARKER}}}, null, [{MARKER}]]
    active: true
  - id: mapa
    nome: Agente com tools_ids mapa
    factoryIaModel: ollama
    model: m-mapa
    prompt: p
    tools_ids: {{a: {MARKER}}}
    active: true
teams:
  - id: time
    nome: Time
    factoryIaModel: ollama
    model: m-time
    prompt: p
    memberIds: [sujo, 3, {{x: {MARKER}}}, mapa]
    mode: route
    active: true
  - id: time-vazio
    nome: Time sem membro de texto
    factoryIaModel: ollama
    model: m-time2
    prompt: p
    memberIds: [1, null]
    active: true
tools:
  - id: cep
    name: CEP
    description: Busca endereço pelo CEP
    route: {BASE}/cep/{{cep}}
    http_method: GET
    parameters:
      - {{name: cep, type: string, description: CEP, required: true}}
    active: true
"""


def _tool_call() -> ModelResponse:
    arguments = json.dumps({"cep": "01001000"})
    return ModelResponse(
        role="assistant",
        tool_calls=[{"id": "c1", "type": "function", "function": {"name": "cep", "arguments": arguments}}],
    )


@contextmanager
def _dirty_app(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, scripts: dict[str, list[str | ModelResponse]] | None = None
) -> Iterator[tuple[_World, RecordingLogger, Any]]:
    logger = RecordingLogger()
    with (
        patch.object(di, "StructlogLoggerAdapter", lambda name="app": logger),
        yaml_app(monkeypatch, tmp_path, CONFIG, scripts=scripts) as (world, _, http),
    ):
        yield world, logger, http


def _errors(logger: RecordingLogger) -> list[tuple[str, dict[str, Any]]]:
    cleanup = (NOT_TEXT_ITEMS, NOT_A_LIST)
    return [(r.message, r.context) for r in logger.records if r.level == "error" and r.message in cleanup]


@pytest.mark.usefixtures("offline_knowledge")
def test_agente_com_tools_ids_sujo_sobe_e_e_servido_com_a_tool_valida(monkeypatch, tmp_path):
    scripts: dict[str, list[str | ModelResponse]] = {"m-sujo": [_tool_call(), "feito"]}
    with _dirty_app(monkeypatch, tmp_path, scripts) as (world, logger, http):
        listed = http.get("/agents", headers=RUN)
        run = http.post("/agents/sujo/runs", data={"message": "oi", "stream": "false"}, headers=RUN)
        run_mapa = http.post("/agents/mapa/runs", data={"message": "oi", "stream": "false"}, headers=RUN)
        teams = http.get("/teams", headers=RUN)

    assert sorted(a["id"] for a in listed.json()) == ["mapa", "sujo"]
    assert run.status_code == 200, run.text
    assert run.json()["content"] == "feito"
    assert [str(r.url) for r in world.requests] == [f"{BASE}/cep/01001000"]
    assert run_mapa.status_code == 200, run_mapa.text
    by_id = {a["id"]: a for a in listed.json()}
    assert [t["name"] for t in by_id["sujo"]["tools"]["tools"]] == ["cep"]  # só a tool de texto da lista suja
    assert "tools" not in by_id["mapa"]  # tools_ids que não é lista: agente sem tools
    # o team com memberIds sujo sobe com os membros de texto; o sem nenhum membro de texto é recusado (isolado)
    assert [t["id"] for t in teams.json()] == ["time"]
    assert MARKER not in listed.text + run.text + run_mapa.text + teams.text
    assert all(MARKER not in repr(r) for r in logger.records)


@pytest.mark.usefixtures("offline_knowledge")
def test_startup_com_ids_sujos_loga_um_registro_por_campo_e_nunca_o_valor(monkeypatch, tmp_path):
    with _dirty_app(monkeypatch, tmp_path) as (_, logger, http):
        http.get("/agents", headers=RUN)

    by_doc = {}
    for message, ctx in _errors(logger):
        by_doc.setdefault((ctx.get("agent_id") or ctx.get("team_id"), ctx["field"]), []).append((message, ctx))
    # o mesmo documento pode ser lido mais de uma vez no startup (agentes e membros de team): sempre o mesmo log
    assert set(by_doc) == {
        ("sujo", "tools_ids"), ("mapa", "tools_ids"), ("time", "memberIds"), ("time-vazio", "memberIds")
    }
    assert by_doc[("sujo", "tools_ids")][0] == (
        NOT_TEXT_ITEMS,
        {"agent_id": "sujo", "field": "tools_ids", "dropped_count": 4, "item_positions": [2, 3, 4, 5]},
    )
    assert by_doc[("mapa", "tools_ids")][0] == (
        NOT_A_LIST,
        {"agent_id": "mapa", "field": "tools_ids", "value_type": "dict"},
    )
    assert by_doc[("time", "memberIds")][0] == (
        NOT_TEXT_ITEMS,
        {"team_id": "time", "field": "memberIds", "dropped_count": 2, "item_positions": [2, 3]},
    )
    for entries in by_doc.values():
        assert all(entry == entries[0] for entry in entries)
    assert all(MARKER not in repr(r) for r in logger.records)
