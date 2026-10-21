"""Mapper de documento de config no modo estrito (escrita do FA-02) e no tolerante (leitura legada).

Estrito: o que a leitura coage ou descarta vira ``InvalidField`` com o campo (e as posições do corpo,
nas listas), nunca com o valor. Tolerante: os mesmos documentos continuam carregando como antes.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.domain.entities.tool import HttpMethod, ParameterType
from src.domain.entities.validation import InvalidField
from src.infrastructure.repositories.config_documents import (
    entity_document,
    map_agent_document,
    map_team_document,
    map_tool_document,
)
from tests.fakes.logger import RecordingLogger

PASTED_VALUE = "sk-valor-de-teste-que-parece-segredo-123456"
AGENT: dict[str, Any] = {"id": "a1", "nome": "A", "factory_ia_model": "ollama", "model": "m", "prompt": "p"}
TEAM: dict[str, Any] = {"id": "t1", "nome": "T", "factory_ia_model": "ollama", "model": "m", "member_ids": ["a1"]}
TOOL: dict[str, Any] = {
    "id": "cep",
    "name": "cep",
    "description": "Busca endereço",
    "route": "https://api.example.invalid/cep/{cep}",
    "parameters": [{"name": "cep", "type": "string", "description": "CEP"}],
}


def _without(doc: dict[str, Any], key: str) -> dict[str, Any]:
    return {k: v for k, v in doc.items() if k != key}


def _refusal(map_document: Any, doc: dict[str, Any]) -> InvalidField:
    with pytest.raises(InvalidField) as caught:
        map_document(doc)
    assert PASTED_VALUE not in str(caught.value)
    return caught.value


@pytest.mark.parametrize(
    ("doc", "field", "indexes"),
    [
        ({**AGENT, "tools_ids": [123, "cep", "fantasma"]}, "tools_ids", (0,)),
        ({**AGENT, "tools_ids": "cep"}, "tools_ids", ()),
        ({**AGENT, "prompt": {"x": 1}}, "prompt", ()),
        ({**AGENT, "active": "false"}, "active", ()),
        ({**AGENT, "summary_active": 1}, "summary_active", ()),
        (_without(AGENT, "factory_ia_model"), "factory_ia_model", ()),
        ({**AGENT, "nome": [PASTED_VALUE]}, "nome", ()),
        ({**AGENT, "api_key_ref": PASTED_VALUE}, "api_key_ref", ()),
        ({**AGENT, "base_url": f"https://{PASTED_VALUE}@host.example"}, "base_url", ()),
        ({**AGENT, "model_params": {"t": {"v": PASTED_VALUE}}}, "model_params", ()),
        ({**AGENT, "rag_config": "sim"}, "rag_config", ()),
        ({**AGENT, "rag_config": {"active": "sim"}}, "rag_config.active", ()),
        ({**AGENT, "rag_config": {"active": True, "search_strategy": PASTED_VALUE}}, "rag_config.search_strategy", ()),
        ({**AGENT, "rag_config": {"active": True, "model": 7}}, "rag_config.model", ()),
        ({**AGENT, "apiKeyRef": "env:OPENAI_API_KEY"}, "apiKeyRef", ()),
        ({**AGENT, "baseUrl": "https://x.example"}, "baseUrl", ()),
        ({**_without(AGENT, "prompt"), "promt": PASTED_VALUE}, "promt", ()),
        ({**AGENT, "rag_config": {"active": True, "modelParams": {"d": 1}}}, "rag_config.modelParams", ()),
        ({**AGENT, "updated_at": "2026-01-01"}, "updated_at", ()),
        ({**AGENT, "promt\nlevel=critical": 1}, "unknown_key", ()),
        ({**AGENT, "k" * 65: 1}, "unknown_key", ()),
        ({**AGENT, "rag_config": {"active": True, "chave com espaço": 1}}, "rag_config.unknown_key", ()),
        ({**AGENT, "markdown": "false"}, "markdown", ()),
        ({**AGENT, "reasoning": 1}, "reasoning", ()),
        ({**AGENT, "add_datetime_to_context": None}, "add_datetime_to_context", ()),
        ({**AGENT, "num_history_runs": PASTED_VALUE}, "num_history_runs", ()),
        ({**AGENT, "num_history_runs": 0}, "num_history_runs", ()),
        ({**AGENT, "numHistoryRuns": 3}, "numHistoryRuns", ()),
    ],
)
def test_agente_estrito_recusa_com_o_campo_o_que_a_leitura_coage(
    doc: dict[str, Any], field: str, indexes: tuple[int, ...]
):
    logger = RecordingLogger()

    refusal = _refusal(lambda d: map_agent_document(d, logger, strict=True), doc)

    assert (refusal.field, refusal.indexes) == (field, indexes)
    assert logger.records == []


@pytest.mark.parametrize(
    ("doc", "field", "indexes"),
    [
        (_without(TEAM, "factory_ia_model"), "factory_ia_model", ()),
        ({**TEAM, "member_ids": "a1"}, "member_ids", ()),
        ({**TEAM, "member_ids": ["a1", 2]}, "member_ids", (1,)),
        ({**TEAM, "member_ids": []}, "member_ids", ()),
        ({**TEAM, "mode": PASTED_VALUE}, "mode", ()),
        ({**TEAM, "prompt": 1}, "prompt", ()),
        ({**TEAM, "user_memory_active": "sim"}, "user_memory_active", ()),
        ({**TEAM, "modelParams": {"t": 1}}, "modelParams", ()),
        ({**TEAM, "markdown": 1}, "markdown", ()),
        ({**TEAM, "add_history_to_context": "false"}, "add_history_to_context", ()),
        ({**TEAM, "num_history_runs": 3}, "num_history_runs", ()),
    ],
)
def test_team_estrito_recusa_com_o_campo(doc: dict[str, Any], field: str, indexes: tuple[int, ...]):
    logger = RecordingLogger()

    refusal = _refusal(lambda d: map_team_document(d, logger, strict=True), doc)

    assert (refusal.field, refusal.indexes) == (field, indexes)
    assert logger.records == []


@pytest.mark.parametrize(
    ("doc", "field", "indexes"),
    [
        ({**TOOL, "name": 123}, "name", ()),
        ({**TOOL, "description": ""}, "description", ()),
        ({**TOOL, "route": 1}, "route", ()),
        ({**TOOL, "instructions": 1}, "instructions", ()),
        ({**TOOL, "active": "sim"}, "active", ()),
        ({**TOOL, "http_method": 7}, "http_method", ()),
        ({**TOOL, "http_method": PASTED_VALUE}, "http_method", ()),
        ({**TOOL, "headers": {"X-Api-Key": 1}}, "headers", ()),
        ({**TOOL, "parameters": {"cep": "string"}}, "parameters", ()),
        ({**TOOL, "parameters": [{"name": "cep", "type": 9, "description": "CEP"}]}, "parameters", (0,)),
        ({**TOOL, "parameters": [TOOL["parameters"][0], {"name": "uf", "description": "UF"}]}, "parameters", (1,)),
        ({**TOOL, "parameters": [TOOL["parameters"][0], "uf"]}, "parameters", (1,)),
        ({**TOOL, "parameters": [{"name": 5, "type": "string", "description": "CEP"}]}, "parameters", (0,)),
        ({**TOOL, "parameters": [{**TOOL["parameters"][0], "required": "sim"}]}, "parameters", (0,)),
        ({**TOOL, "methd": "POST"}, "methd", ()),
        ({**TOOL, "parameters": [{**TOOL["parameters"][0], "requird": True}]}, "parameters", (0,)),
    ],
)
def test_tool_estrita_recusa_com_o_campo(doc: dict[str, Any], field: str, indexes: tuple[int, ...]):
    refusal = _refusal(lambda d: map_tool_document(d, strict=True), doc)

    assert (refusal.field, refusal.indexes) == (field, indexes)


def test_documento_valido_mapeia_igual_nos_dois_modos():
    logger = RecordingLogger()
    agent = {**AGENT, "tools_ids": ["cep"], "rag_config": {"active": True, "doc_name": "d"}}
    tool = {**TOOL, "http_method": "post", "parameters": [{**TOOL["parameters"][0], "type": "INTEGER"}]}

    assert map_agent_document(agent, logger, strict=True) == map_agent_document(agent, logger)
    assert map_team_document(TEAM, logger, strict=True) == map_team_document(TEAM, logger)
    assert map_tool_document(tool, strict=True) == map_tool_document(tool)
    assert logger.records == []


def test_estrito_aceita_as_grafias_legadas_que_o_mapper_le():
    """Com as duas grafias no corpo, vale a snake_case, como na leitura (README)."""
    logger = RecordingLogger()
    team = {**_without(_without(TEAM, "factory_ia_model"), "member_ids"), "factoryIaModel": "ollama"}
    team.update(memberIds=["a1"], userMemoryActive=False, summaryActive=True)
    agent = {**_without(AGENT, "factory_ia_model"), "factoryIaModel": "ollama"}
    agent["rag_config"] = {"active": True, "factoryIaModel": "ollama", "model": "nomic-embed-text"}

    both_spellings = {**TEAM, "memberIds": ["fantasma"], "factoryIaModel": "openai"}

    assert map_team_document(team, logger, strict=True) == map_team_document(team, logger)
    assert map_team_document(both_spellings, logger, strict=True) == map_team_document(TEAM, logger)
    assert map_agent_document(agent, logger, strict=True) == map_agent_document(agent, logger)


def test_documento_gravado_pela_escrita_e_lido_de_volta_no_modo_estrito():
    """Toda chave que a escrita grava é lida pelo mapper: o ciclo GET -> PUT não esbarra em chave desconhecida."""
    logger = RecordingLogger()
    rag = {"active": True, "doc_name": "d", "model": "m", "factory_ia_model": "openai", "base_url": "https://e.example"}
    agent = map_agent_document({**AGENT, "tools_ids": ["cep"], "rag_config": rag, "model_params": {"t": 1}}, logger)
    team = map_team_document({**TEAM, "descricao": "d", "api_key_ref": "env:OPENAI_API_KEY"}, logger)
    tool = map_tool_document({**TOOL, "headers": {"X-Api-Key": "v"}, "instructions": "i"})

    assert map_agent_document(entity_document(agent), logger, strict=True) == agent
    assert map_team_document(entity_document(team), logger, strict=True) == team
    assert map_tool_document(entity_document(tool), strict=True) == tool


def test_leitura_legada_continua_tolerante_com_o_que_a_escrita_recusa():
    """Documento já gravado continua carregando: a recusa é só do caminho de escrita."""
    logger = RecordingLogger()
    legacy_agent = {**_without(AGENT, "factory_ia_model"), "tools_ids": "cep", "active": "false", "prompt": {"x": 1}}
    legacy_tool = {**TOOL, "name": 123, "http_method": 7, "parameters": [{"name": "p", "type": 9, "description": "d"}]}

    agent = map_agent_document(legacy_agent, logger)
    tool = map_tool_document(legacy_tool)

    assert (agent.factory_ia_model, agent.tools_ids, agent.active) == ("ollama", [], "false")
    assert (tool.name, tool.http_method, tool.parameters[0].type) == (123, HttpMethod.GET, ParameterType.STRING)


# FA-07: campos de execução (antes fixos na montagem), só em snake_case, com o default de antes.
AGENT_RUN_OPTIONS: dict[str, Any] = {
    "markdown": False,
    "add_history_to_context": False,
    "add_datetime_to_context": False,
    "num_history_runs": 12,
    "reasoning": True,
}
TEAM_RUN_OPTIONS: dict[str, Any] = {"markdown": False, "add_history_to_context": False}


def test_documento_sem_os_campos_de_execucao_mapeia_para_os_defaults_de_antes():
    logger = RecordingLogger()

    agent = map_agent_document(AGENT, logger)
    team = map_team_document(TEAM, logger)

    assert (agent.markdown, agent.add_history_to_context, agent.add_datetime_to_context) == (True, True, True)
    assert (agent.num_history_runs, agent.reasoning) == (5, False)
    assert (team.markdown, team.add_history_to_context) == (True, True)
    assert map_agent_document(AGENT, logger, strict=True) == agent
    assert map_team_document(TEAM, logger, strict=True) == team


def test_campos_de_execucao_sao_lidos_e_gravados_em_snake_case():
    logger = RecordingLogger()

    agent = map_agent_document({**AGENT, **AGENT_RUN_OPTIONS}, logger, strict=True)
    team = map_team_document({**TEAM, **TEAM_RUN_OPTIONS}, logger, strict=True)

    assert {key: getattr(agent, key) for key in AGENT_RUN_OPTIONS} == AGENT_RUN_OPTIONS
    assert {key: getattr(team, key) for key in TEAM_RUN_OPTIONS} == TEAM_RUN_OPTIONS
    assert entity_document(agent).items() >= AGENT_RUN_OPTIONS.items()
    assert entity_document(team).items() >= TEAM_RUN_OPTIONS.items()
    assert map_agent_document(entity_document(agent), logger, strict=True) == agent
    assert map_team_document(entity_document(team), logger, strict=True) == team


@pytest.mark.parametrize(
    "doc",
    [{**AGENT, "num_history_runs": "5"}, {**AGENT, "reasoning": "false"}],
)
def test_leitura_tolerante_tambem_recusa_campo_de_execucao_invalido(doc: dict[str, Any]):
    """Campo novo, sem documento legado a preservar: a entidade recusa nos dois modos (o repositório isola
    o documento com log) em vez de deixar ``"false"`` ligar o raciocínio ou ``"5"`` quebrar o agno."""
    with pytest.raises(InvalidField):
        map_agent_document(doc, RecordingLogger())
