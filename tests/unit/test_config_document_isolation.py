"""F1-10 (B7): documento inválido em ``agents_config``/``teams_config`` isola só ele.

Antes, ``get_active_agents``/``get_active_teams`` mapeavam tudo numa list comprehension: um
documento inválido abortava a lista e derrubava o startup. Agora cada inválido vira log de
erro com o id (se for texto) e o tipo do erro (nunca o documento: pode ter segredo/PII) e os
válidos carregam. Repositórios reais sobre a coleção em memória de ``tests/fakes``.
"""

from __future__ import annotations

from datetime import UTC
from typing import Any

import pytest
from bson import ObjectId

from src.infrastructure.repositories import mongo_base
from src.infrastructure.repositories.mongo_agent_config_repository import MongoAgentConfigRepository
from src.infrastructure.repositories.mongo_team_config_repository import MongoTeamConfigRepository
from tests.fakes import FakeMongoClient, FakeMongoCollection, RecordingLogger

MARKER = "SEGREDO-QUE-NAO-PODE-IR-AO-LOG"


def _agent_doc(agent_id: Any = "ok", **overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "id": agent_id,
        "nome": "Agente",
        "factory_ia_model": "ollama",
        "model": "m",
        "descricao": "d",
        "prompt": MARKER,
        "active": True,
    }
    doc.update(overrides)
    return doc


def _team_doc(team_id: Any = "time", **overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "id": team_id,
        "nome": "Time",
        "factoryIaModel": "ollama",
        "model": "m",
        "prompt": MARKER,
        "member_ids": ["ok"],
        "mode": "route",
        "active": True,
    }
    doc.update(overrides)
    return doc


def _without(doc: dict[str, Any], *keys: str) -> dict[str, Any]:
    return {k: v for k, v in doc.items() if k not in keys}


def _install(monkeypatch: pytest.MonkeyPatch, **collections: FakeMongoCollection) -> None:
    client = FakeMongoClient(collections)
    monkeypatch.setattr(mongo_base.MongoClientFactory, "get_client", classmethod(lambda cls, _conn: client))


def _agent_repo(monkeypatch: pytest.MonkeyPatch, docs: list[dict[str, Any]], logger: RecordingLogger):
    _install(monkeypatch, agents_config=FakeMongoCollection(docs))
    return MongoAgentConfigRepository(connection_string="mongodb://mongo.invalid:27017", logger=logger)


def _team_repo(monkeypatch: pytest.MonkeyPatch, docs: list[dict[str, Any]], logger: RecordingLogger):
    _install(monkeypatch, teams_config=FakeMongoCollection(docs))
    return MongoTeamConfigRepository(connection_string="mongodb://mongo.invalid:27017", logger=logger)


def _errors(logger: RecordingLogger) -> list[tuple[str, dict[str, Any]]]:
    return [(r.message, r.context) for r in logger.records if r.level == "error"]


def _assert_secret_not_logged(logger: RecordingLogger) -> None:
    assert all(MARKER not in repr(r) for r in logger.records)


# ── agents_config ────────────────────────────────────────────────────

HOSTILE_AGENT_DOCS = [
    pytest.param(_without(_agent_doc(), "id"), None, "ValueError", id="sem-id"),
    pytest.param(_agent_doc(""), "", "ValueError", id="id-vazio"),
    pytest.param(_agent_doc(123), None, "ValueError", id="id-numero"),
    pytest.param(_agent_doc({"$gt": ""}), None, "ValueError", id="id-objeto"),
    pytest.param(_agent_doc(["a"]), None, "ValueError", id="id-lista"),
    pytest.param(_agent_doc("sem-nome", nome=""), "sem-nome", "ValueError", id="nome-vazio"),
    pytest.param(_agent_doc("nome-objeto", nome={"pt": "x"}), "nome-objeto", "ValueError", id="nome-objeto"),
    pytest.param(_agent_doc("sem-model", model=None), "sem-model", "ValueError", id="model-nulo"),
    pytest.param(_agent_doc("descricao-numero", descricao=7), "descricao-numero", "ValueError", id="descricao-numero"),
    pytest.param(
        _agent_doc("descricao-objeto", descricao={"pt": "x"}), "descricao-objeto", "ValueError", id="descricao-objeto"
    ),
    pytest.param(_agent_doc("rag-texto", rag_config="sim"), "rag-texto", "AttributeError", id="rag-config-texto"),
    pytest.param(
        _agent_doc("rag-estrategia", rag_config={"active": True, "search_strategy": "vetorial"}),
        "rag-estrategia",
        "ValueError",
        id="search-strategy-invalida",
    ),
]


@pytest.mark.parametrize(("bad_doc", "logged_id", "error_type"), HOSTILE_AGENT_DOCS)
async def test_agente_invalido_vira_log_com_id_e_os_validos_carregam(
    monkeypatch: pytest.MonkeyPatch, bad_doc: dict[str, Any], logged_id: str | None, error_type: str
):
    logger = RecordingLogger()
    docs = [_agent_doc("antes"), bad_doc, _agent_doc("depois")]
    repo = _agent_repo(monkeypatch, docs, logger)

    configs = await repo.get_active_agents()

    assert [c.id for c in configs] == ["antes", "depois"]
    assert _errors(logger) == [
        (
            "Documento de agente inválido ignorado",
            {"agent_id": logged_id, "mongo_id": None, "error_type": error_type},
        )
    ]
    _assert_secret_not_logged(logger)


async def test_agente_invalido_sem_id_loga_o_object_id_do_mongo(monkeypatch: pytest.MonkeyPatch):
    logger = RecordingLogger()
    oid = ObjectId()
    repo = _agent_repo(monkeypatch, [{**_without(_agent_doc(), "id"), "_id": oid}], logger)

    assert await repo.get_active_agents() == []
    assert _errors(logger) == [
        ("Documento de agente inválido ignorado", {"agent_id": None, "mongo_id": str(oid), "error_type": "ValueError"})
    ]


async def test_documento_legado_do_seed_de_agente_continua_valido(monkeypatch: pytest.MonkeyPatch):
    """Formato do seed/README: ``factoryIaModel`` camelCase, ``prompt`` em lista, RAG desligado."""
    logger = RecordingLogger()
    legacy = _without(_agent_doc("legado"), "factory_ia_model") | {
        "factoryIaModel": "openai",
        "prompt": ["linha 1", "linha 2"],
        "rag_config": {"active": False},
        "tools_ids": [],
    }
    repo = _agent_repo(monkeypatch, [legacy], logger)

    (config,) = await repo.get_active_agents()

    assert (config.id, config.factory_ia_model, config.prompt) == ("legado", "openai", ["linha 1", "linha 2"])
    assert _errors(logger) == []


async def test_todos_os_agentes_invalidos_devolve_lista_vazia_sem_levantar(monkeypatch: pytest.MonkeyPatch):
    logger = RecordingLogger()
    bad = [p.values[0] for p in HOSTILE_AGENT_DOCS]
    repo = _agent_repo(monkeypatch, bad, logger)

    assert await repo.get_active_agents() == []
    assert len(_errors(logger)) == len(bad)


async def test_falha_do_banco_ao_listar_agentes_continua_subindo(monkeypatch: pytest.MonkeyPatch):
    """Isolar documento não pode engolir falha de I/O do cursor."""
    logger = RecordingLogger()

    class _Broken(FakeMongoCollection):
        def find(self, query: dict[str, Any]):
            raise RuntimeError("conexão perdida")

    _install(monkeypatch, agents_config=_Broken())
    repo = MongoAgentConfigRepository(connection_string="mongodb://mongo.invalid:27017", logger=logger)

    with pytest.raises(RuntimeError, match="conexão perdida"):
        await repo.get_active_agents()


# ── teams_config ─────────────────────────────────────────────────────

HOSTILE_TEAM_DOCS = [
    pytest.param(_without(_team_doc(), "id"), None, "ValueError", id="sem-id"),
    pytest.param(_team_doc(["t"]), None, "ValueError", id="id-lista"),
    pytest.param(_team_doc(7), None, "ValueError", id="id-numero"),
    pytest.param(_team_doc("nome-numero", nome=5), "nome-numero", "ValueError", id="nome-numero"),
    pytest.param(_without(_team_doc("sem-membros"), "member_ids"), "sem-membros", "ValueError", id="sem-membros"),
    pytest.param(_team_doc("modo", mode="aleatorio"), "modo", "ValueError", id="modo-invalido"),
    pytest.param(_team_doc("descricao-lista", descricao=["x"]), "descricao-lista", "ValueError", id="descricao-lista"),
]


@pytest.mark.parametrize(("bad_doc", "logged_id", "error_type"), HOSTILE_TEAM_DOCS)
async def test_team_invalido_vira_log_com_id_e_os_validos_carregam(
    monkeypatch: pytest.MonkeyPatch, bad_doc: dict[str, Any], logged_id: str | None, error_type: str
):
    logger = RecordingLogger()
    docs = [_team_doc("antes"), bad_doc, _team_doc("depois")]
    repo = _team_repo(monkeypatch, docs, logger)

    configs = await repo.get_active_teams()

    assert [c.id for c in configs] == ["antes", "depois"]
    assert _errors(logger) == [
        ("Documento de team inválido ignorado", {"team_id": logged_id, "mongo_id": None, "error_type": error_type})
    ]
    _assert_secret_not_logged(logger)


async def test_team_no_formato_legado_camelcase_continua_valido(monkeypatch: pytest.MonkeyPatch):
    logger = RecordingLogger()
    legacy = _without(_team_doc("legado"), "member_ids") | {
        "memberIds": ["a", "b"],
        "userMemoryActive": False,
        "summaryActive": True,
    }
    repo = _team_repo(monkeypatch, [legacy], logger)

    (config,) = await repo.get_active_teams()

    assert (config.member_ids, config.user_memory_active, config.summary_active) == (["a", "b"], False, True)
    assert _errors(logger) == []


# ── ordem determinística: o documento mais antigo (menor _id) vem primeiro ──


def _oids() -> tuple[ObjectId, ObjectId]:
    from datetime import datetime

    older = ObjectId.from_datetime(datetime(2024, 1, 1, tzinfo=UTC))
    newer = ObjectId.from_datetime(datetime(2025, 1, 1, tzinfo=UTC))
    return older, newer


async def test_agentes_vem_em_ordem_de_id_do_mongo_e_o_mais_antigo_vence_o_id_repetido(
    monkeypatch: pytest.MonkeyPatch,
):
    from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase

    logger = RecordingLogger()
    older, newer = _oids()
    docs = [_agent_doc("dup", nome="novo", _id=newer), _agent_doc("dup", nome="antigo", _id=older)]
    repo = _agent_repo(monkeypatch, docs, logger)

    class _Factory:
        async def create_agent(self, config):  # type: ignore[no-untyped-def]
            return config.nome

    assert [c.nome for c in await repo.get_active_agents()] == ["antigo", "novo"]
    assert await GetActiveAgentsUseCase(_Factory(), repo, logger).execute() == ["antigo"]  # type: ignore[arg-type]


async def test_teams_vem_em_ordem_de_id_do_mongo(monkeypatch: pytest.MonkeyPatch):
    logger = RecordingLogger()
    older, newer = _oids()
    docs = [_team_doc("dup", nome="novo", _id=newer), _team_doc("dup", nome="antigo", _id=older)]
    repo = _team_repo(monkeypatch, docs, logger)

    assert [c.nome for c in await repo.get_active_teams()] == ["antigo", "novo"]
