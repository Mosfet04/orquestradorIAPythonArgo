"""QA do F2-06: documentos hostis (tipo errado em cada campo) dão o mesmo resultado no Mongo e no YAML.

Mesmo documento, mesmos válidos carregados, mesmos inválidos isolados (contagem e tipo do erro no
log) e nunca o conteúdo no log. Complementa ``test_config_documents_contract.py``, que cobre os
inválidos "comportados"; aqui os campos trazem tipos que o YAML permite escrever à mão (número, lista,
mapa, booleano, data, nulo) e que o BSON também aceitaria.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from tests.fakes.config_stores import DOCUMENT_BACKENDS, document_backend
from tests.fakes.logger import RecordingLogger

MARKER = "SEGREDO-HOSTIL-QA-F206"

VALID_AGENT: dict[str, Any] = {"id": "ok", "nome": "Ok", "model": "m", "prompt": "p", "active": True}
VALID_TEAM: dict[str, Any] = {"id": "tok", "nome": "T", "model": "m", "member_ids": ["ok"], "active": True}
VALID_TOOL: dict[str, Any] = {
    "id": "tl", "name": "n", "description": "d", "route": "https://x.example/a", "http_method": "GET", "active": True,
}

AGENT_FIELDS = {
    "id-numero": {"id": 5},
    "id-lista": {"id": [MARKER]},
    "id-mapa": {"id": {"$ne": MARKER}},
    "id-nulo": {"id": None},
    "nome-booleano": {"nome": True},
    "nome-lista": {"nome": [MARKER]},
    "model-numero": {"model": 3},
    "prompt-mapa": {"prompt": {"a": MARKER}},
    "factory-lista": {"factoryIaModel": [MARKER]},
    "tools_ids-mapa": {"tools_ids": {"a": MARKER}},
    "rag-texto": {"rag_config": MARKER},
    "rag-lista": {"rag_config": [MARKER]},
    "rag-strategy": {"rag_config": {"active": True, "doc_name": "d", "search_strategy": MARKER}},
    "model_params-texto": {"model_params": MARKER},
    "model_params-lista": {"model_params": [MARKER]},
    "base_url-numero": {"base_url": 7},
    "api_key_ref-numero": {"api_key_ref": 7},
    "memory-texto": {"user_memory_active": MARKER},
}
TEAM_FIELDS = {
    "id-numero": {"id": 5},
    "mode-numero": {"mode": 3},
    "mode-invalido": {"mode": MARKER},
    "member_ids-numero": {"member_ids": 3},
    "model-lista": {"model": [MARKER]},
    "model_params-texto": {"model_params": MARKER},
}
TOOL_FIELDS = {
    "id-numero": {"id": 5},
    "parameters-numero": {"parameters": 3},
    "parameters-texto": {"parameters": [MARKER]},
    "parameters-sem-nome": {"parameters": [{"type": "string"}]},
    "parameters-tipo-invalido": {"parameters": [{"name": "a", "type": MARKER}]},
    "method-invalido": {"http_method": MARKER},
    "method-numero": {"http_method": 3},
    "route-vazia": {"route": ""},
    "headers-texto": {"headers": MARKER},
}


async def _load(kind: str, name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, docs: list[Any]):
    backend = document_backend(name, monkeypatch, tmp_path)
    logger = RecordingLogger()
    if kind == "agents":
        result = await backend.agents(docs, logger).get_active_agents()
    elif kind == "teams":
        result = await backend.teams(docs, logger).get_active_teams()
    else:
        result = await backend.tools(docs, logger).get_all_active_tools()
    errors = sorted((r.message, r.context.get("error_type")) for r in logger.records if r.level == "error")
    assert all(MARKER not in repr(r) for r in logger.records), "conteúdo do documento foi para o log"
    return result, errors


@pytest.mark.parametrize(
    ("kind", "valid", "fields"),
    [("agents", VALID_AGENT, AGENT_FIELDS), ("teams", VALID_TEAM, TEAM_FIELDS), ("tools", VALID_TOOL, TOOL_FIELDS)],
)
async def test_campo_com_tipo_errado_da_o_mesmo_resultado_no_mongo_e_no_yaml(
    kind: str, valid: dict[str, Any], fields: dict[str, dict[str, Any]], monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    docs = [{**valid, **hostile} for hostile in fields.values()] + [valid]
    # Os hostis partem do documento válido (mesmo id): a listagem pode repetir ids (agents/teams) ou
    # colapsá-los (tools); o que importa é o resultado igual nos dois backends.
    outcomes = {}
    for name in DOCUMENT_BACKENDS:
        sub = tmp_path / name
        sub.mkdir()
        outcomes[name] = await _load(kind, name, monkeypatch, sub, docs)

    mongo, yaml_ = outcomes["mongo"], outcomes["yaml"]
    assert yaml_[0] == mongo[0]
    assert yaml_[1] == mongo[1]
    assert valid["id"] in [item.id for item in yaml_[0]]
    # nenhum campo hostil derruba o lote (o último documento, válido, sempre carrega) e há recusas de fato
    assert yaml_[1], "nenhum documento hostil foi recusado: o caso não exercita o isolamento"


@pytest.mark.parametrize("name", DOCUMENT_BACKENDS)
async def test_lote_so_com_documentos_invalidos_devolve_vazio_e_loga_cada_um(
    name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    docs = [{**VALID_AGENT, "id": f"x{i}", "nome": 5} for i in range(5)]

    result, errors = await _load("agents", name, monkeypatch, tmp_path, docs)

    assert result == []
    assert errors == [("Documento de agente inválido ignorado", "ValueError")] * 5
