"""F2-01: mappers Mongo -> ``ModelConfig``.

Documento antigo gera o mesmo provider/model_id de antes (``factory_ia_model`` ou
``factoryIaModel`` + ``model``; RAG com os defaults de sempre). Campos novos opcionais:
``model_params``, ``base_url`` e ``api_key_ref`` (agente, ``rag_config`` e team). Documento com
campo novo inválido é isolado como os demais inválidos (F1-10) e o log não traz o valor.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.domain.entities.model_config import ModelConfig
from src.infrastructure.repositories import mongo_base
from src.infrastructure.repositories.mongo_agent_config_repository import MongoAgentConfigRepository
from src.infrastructure.repositories.mongo_team_config_repository import MongoTeamConfigRepository
from tests.fakes import FakeMongoClient, FakeMongoCollection, RecordingLogger

MARKER = "sk-VALOR-QUE-NAO-PODE-IR-AO-LOG"


def _agent_doc(agent_id: str = "a", **extra: Any) -> dict[str, Any]:
    return {"id": agent_id, "nome": "Agente", "model": "llama3.2:latest", "prompt": "p", "active": True, **extra}


def _team_doc(team_id: str = "t", **extra: Any) -> dict[str, Any]:
    return {"id": team_id, "nome": "Time", "model": "qwen3", "member_ids": ["a"], "active": True, **extra}


def _install(monkeypatch: pytest.MonkeyPatch, **collections: FakeMongoCollection) -> None:
    client = FakeMongoClient(collections)
    monkeypatch.setattr(mongo_base.MongoClientFactory, "get_client", classmethod(lambda cls, _conn: client))


async def _load_agents(monkeypatch: pytest.MonkeyPatch, docs: list[dict[str, Any]], logger: RecordingLogger):
    _install(monkeypatch, agents_config=FakeMongoCollection(docs))
    repo = MongoAgentConfigRepository(connection_string="mongodb://mongo.invalid:27017", logger=logger)
    return await repo.get_active_agents()


async def _load_teams(monkeypatch: pytest.MonkeyPatch, docs: list[dict[str, Any]], logger: RecordingLogger):
    _install(monkeypatch, teams_config=FakeMongoCollection(docs))
    repo = MongoTeamConfigRepository(connection_string="mongodb://mongo.invalid:27017", logger=logger)
    return await repo.get_active_teams()


@pytest.mark.parametrize(
    ("provider_fields", "expected_provider"),
    [
        ({"factory_ia_model": "gemini"}, "gemini"),
        ({"factoryIaModel": "gemini"}, "gemini"),
        ({"factory_ia_model": "openai", "factoryIaModel": "gemini"}, "openai"),
        ({}, "ollama"),
    ],
    ids=["snake", "camel", "as-duas-vale-snake", "sem-provider-default-ollama"],
)
async def test_agente_legado_gera_o_mesmo_provider_e_model_id(
    monkeypatch: pytest.MonkeyPatch, provider_fields: dict[str, str], expected_provider: str
):
    [config] = await _load_agents(monkeypatch, [_agent_doc(**provider_fields)], RecordingLogger())

    assert config.factory_ia_model == expected_provider
    assert config.model_config == ModelConfig(provider=expected_provider, model_id="llama3.2:latest")


async def test_rag_legado_mantem_os_defaults_do_embedder(monkeypatch: pytest.MonkeyPatch):
    docs = [
        _agent_doc("sem-modelo", rag_config={"active": True}),
        _agent_doc("camel", rag_config={"active": True, "model": "gemini-embedding-001", "factoryIaModel": "gemini"}),
    ]

    sem_modelo, camel = await _load_agents(monkeypatch, docs, RecordingLogger())

    assert sem_modelo.rag_config.model_config == ModelConfig(provider="ollama", model_id="nomic-embed-text:latest")
    assert camel.rag_config.model_config == ModelConfig(provider="gemini", model_id="gemini-embedding-001")


async def test_agente_e_rag_com_campos_novos(monkeypatch: pytest.MonkeyPatch):
    doc = _agent_doc(
        factory_ia_model="openai_compatible",
        model_params={"temperature": 0.2, "max_tokens": 1024},
        base_url="https://llm.example.invalid/v1",
        api_key_ref="env:LLM_API_KEY",
        rag_config={
            "active": True,
            "model": "bge-m3",
            "factory_ia_model": "openai_compatible",
            "model_params": {"dimensions": 1024},
            "base_url": "http://embeddings.internal:8080/v1",
            "api_key_ref": "file:/run/secrets/embeddings",
        },
    )

    [config] = await _load_agents(monkeypatch, [doc], RecordingLogger())

    assert config.model_config == ModelConfig(
        provider="openai_compatible",
        model_id="llama3.2:latest",
        params={"temperature": 0.2, "max_tokens": 1024},
        base_url="https://llm.example.invalid/v1",
        api_key_ref="env:LLM_API_KEY",
    )
    assert config.rag_config.model_config == ModelConfig(
        provider="openai_compatible",
        model_id="bge-m3",
        params={"dimensions": 1024},
        base_url="http://embeddings.internal:8080/v1",
        api_key_ref="file:/run/secrets/embeddings",
    )


@pytest.mark.parametrize(
    "rag_without_explicit_model",
    [
        {"active": True, "api_key_ref": "env:EMB_API_KEY"},
        {"active": True, "model": "bge-m3", "base_url": "http://emb.internal:8080/v1"},
        {"active": True, "factory_ia_model": "openai_compatible", "model_params": {"dimensions": 8}},
    ],
    ids=["sem-model-e-provider", "sem-provider", "sem-model"],
)
async def test_rag_com_campo_novo_nao_herda_os_defaults_do_embedder(
    monkeypatch: pytest.MonkeyPatch, rag_without_explicit_model: dict[str, Any]
):
    """Endpoint/chave novos não podem cair num ollama/nomic implícito: model e provider explícitos."""
    logger = RecordingLogger()
    docs = [_agent_doc("ruim", rag_config=rag_without_explicit_model), _agent_doc("ok")]

    configs = await _load_agents(monkeypatch, docs, logger)

    assert [c.id for c in configs] == ["ok"]
    assert [r.context.get("agent_id") for r in logger.records if r.level == "error"] == ["ruim"]


async def test_campos_novos_nulos_valem_como_ausentes(monkeypatch: pytest.MonkeyPatch):
    doc = _agent_doc(model_params=None, base_url=None, api_key_ref=None)

    [config] = await _load_agents(monkeypatch, [doc], RecordingLogger())

    assert config.model_config == ModelConfig(provider="ollama", model_id="llama3.2:latest")


@pytest.mark.parametrize(
    "bad_fields",
    [
        {"api_key_ref": MARKER},
        {"base_url": f"https://u:{MARKER}@llm.example.invalid"},
        {"model_params": {"api_key": {"value": MARKER}}},
        {"model_params": [MARKER]},
        {"rag_config": {"active": True, "model": "m", "api_key_ref": MARKER}},
        {"rag_config": {"active": True, "model": None, "base_url": "https://emb.example.invalid"}},
    ],
    ids=[
        "api-key-ref-com-o-segredo",
        "base-url-com-credencial",
        "param-aninhado",
        "params-lista",
        "rag-ref",
        "rag-sem-modelo",
    ],
)
async def test_agente_com_campo_novo_invalido_e_isolado_sem_vazar(
    monkeypatch: pytest.MonkeyPatch, bad_fields: dict[str, Any]
):
    logger = RecordingLogger()
    docs = [_agent_doc("antes"), _agent_doc("ruim", **bad_fields), _agent_doc("depois")]

    configs = await _load_agents(monkeypatch, docs, logger)

    assert [c.id for c in configs] == ["antes", "depois"]
    errors = [(r.message, r.context) for r in logger.records if r.level == "error"]
    assert errors == [
        ("Documento de agente inválido ignorado", {"agent_id": "ruim", "mongo_id": None, "error_type": "ValueError"})
    ]
    assert all(MARKER not in repr(r) for r in logger.records)


async def test_team_legado_e_com_campos_novos(monkeypatch: pytest.MonkeyPatch):
    docs = [
        _team_doc("legado", factoryIaModel="ollama"),
        _team_doc(
            "novo", factoryIaModel="openai", base_url="https://gw.example.invalid/v1", api_key_ref="env:GW_API_KEY"
        ),
        _team_doc("ruim", factoryIaModel="openai", api_key_ref=MARKER),
    ]
    logger = RecordingLogger()

    legado, novo = await _load_teams(monkeypatch, docs, logger)

    assert legado.model_config == ModelConfig(provider="ollama", model_id="qwen3")
    assert novo.model_config == ModelConfig(
        provider="openai", model_id="qwen3", base_url="https://gw.example.invalid/v1", api_key_ref="env:GW_API_KEY"
    )
    assert [r.context.get("team_id") for r in logger.records if r.level == "error"] == ["ruim"]
    assert all(MARKER not in repr(r) for r in logger.records)
