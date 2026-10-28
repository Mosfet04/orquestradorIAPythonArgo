"""Escrita nos repositórios de config do Mongo (FA-01): o documento gravado.

O contrato da escrita está em ``tests/contract/test_config_write_contract.py``; aqui, o que só o
Mongo tem: documento legado (camelCase, sem ``version``) regravado em snake_case no mesmo ``_id``,
com ``version`` e ``updated_at``; ``version`` inválida isolando só o documento na listagem; e a
criação que perde a corrida para outra no índice único de ``id`` virando conflito.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from src.domain.entities.agent_config import AgentConfig
from src.domain.repositories.versioning import ConfigVersionConflict, Versioned
from tests.fakes.config_stores import MongoBackend, agent_document
from tests.fakes.logger import RecordingLogger
from tests.fakes.mongo import FakeMongoCollection


def _agent(label: str) -> AgentConfig:
    return AgentConfig(id="a", nome=label, factory_ia_model="openai", model="m", descricao="d", prompt="p")


async def test_save_regrava_documento_legado_em_snake_case_no_mesmo_id(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = MongoBackend(monkeypatch)
    legacy = {"id": "a", "nome": "legado", "factoryIaModel": "ollama", "model": "m", "prompt": "p", "active": True}
    repo = backend.agents([legacy])
    (stored,) = backend.collections["agents_config"].docs
    mongo_id = stored["_id"]

    assert await repo.save(_agent("novo"), 0) == 1

    (stored,) = backend.collections["agents_config"].docs
    updated_at = stored.pop("updated_at")
    assert stored == {"_id": mongo_id, **agent_document(_agent("novo")), "version": 1}
    assert isinstance(updated_at, datetime) and updated_at.utcoffset() is not None


async def test_version_invalida_isola_so_o_documento_na_listagem(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = RecordingLogger()
    docs = [
        {**agent_document(_agent("texto")), "id": "texto", "version": "3"},
        {**agent_document(_agent("negativa")), "id": "negativa", "version": -1},
        {**agent_document(_agent("nula")), "id": "nula", "version": None},
    ]
    repo = MongoBackend(monkeypatch).agents(docs, logger)

    listed = await repo.list_all()

    assert [(v.entity.id, v.version) for v in listed] == [("nula", 0)]
    assert [r.context.get("agent_id") for r in logger.records if r.level == "error"] == ["texto", "negativa"]


async def test_criacao_que_perde_a_corrida_no_indice_unico_conflita(monkeypatch: pytest.MonkeyPatch) -> None:
    """Outra réplica cria o mesmo id entre a verificação e o ``insert_one``: o índice único recusa."""
    backend = MongoBackend(monkeypatch)
    repo = backend.agents([])
    collection = backend.collections["agents_config"]
    collection.unique_keys.add("id")  # o índice do seed (mongo-init/init-db.js)
    rival = {**agent_document(_agent("rival")), "version": 1}

    async def find_one_then_rival_inserts(*args: object, **kwargs: object) -> dict[str, object] | None:
        found = await FakeMongoCollection.find_one(collection, *args, **kwargs)
        await FakeMongoCollection.insert_one(collection, dict(rival))
        return found

    monkeypatch.setattr(collection, "find_one", find_one_then_rival_inserts)

    with pytest.raises(ConfigVersionConflict):
        await repo.save(_agent("perdedora"), 0)

    assert await repo.list_all() == [Versioned(_agent("rival"), 1)]
