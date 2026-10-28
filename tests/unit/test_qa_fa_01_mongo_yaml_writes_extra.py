"""QA FA-01: lacunas específicas de Mongo e YAML na escrita/listagem de config.

Mongo (coleção fake): documento legado camelCase regravado e relido pelo runtime com a mesma
entidade; ``version`` 0 explícita equivale a ausente; ``version`` inválida some só da listagem do
admin e o runtime segue servindo; erro do store propaga sem virar conflito nem vazar o documento;
criação de id novo com o id na coleção de outro tipo. YAML: ``version`` do arquivo na listagem,
``version`` inválida isolada, arquivo intacto depois de recusar escrita.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.tool import HttpMethod, Tool
from src.domain.repositories.versioning import ConfigVersionConflict, Versioned
from src.infrastructure.repositories.mongo_agent_config_repository import MongoAgentConfigRepository
from tests.fakes.config_stores import MongoBackend, YamlBackend, agent_document
from tests.fakes.logger import RecordingLogger
from tests.fakes.mongo import FakeMongoCollection

SECRET_MARKER = "SEGREDO-NO-DOCUMENTO"  # noqa: S105 - marcador de teste, não é credencial


def _agent(entity_id: str = "a", label: str = "v0") -> AgentConfig:
    return AgentConfig(id=entity_id, nome=label, factory_ia_model="openai", model="m", descricao="d", prompt="p")


# ── Mongo: documento legado ─────────────────────────────────────────


async def test_agente_legado_camel_case_relido_pelo_runtime_volta_igual_depois_do_save(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = MongoBackend(monkeypatch)
    legacy = {
        "id": "a", "nome": "legado", "factoryIaModel": "openai", "model": "m", "descricao": "d",
        "prompt": "p", "active": True, "userMemoryActive": True, "campo_morto": "x",
    }
    repo = backend.agents([legacy])
    before = await repo.get_agent_by_id("a")
    assert before.factory_ia_model == "openai"

    assert await repo.save(before, 0) == 1

    assert await repo.get_agent_by_id("a") == before
    assert await repo.get_active_agents() == [before]
    assert await repo.list_all() == [Versioned(before, 1)]
    (stored,) = backend.collections["agents_config"].docs
    assert "factoryIaModel" not in stored and "campo_morto" not in stored and stored["factory_ia_model"] == "openai"


async def test_team_legado_member_ids_camel_case_regravado_em_snake_case(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = MongoBackend(monkeypatch)
    legacy = {
        "id": "t", "nome": "time", "factoryIaModel": "ollama", "model": "m", "memberIds": ["x", "y"],
        "userMemoryActive": False, "summaryActive": True, "active": True,
    }
    repo = backend.teams([legacy])
    before = await repo.get_team_by_id("t")
    assert before is not None and before.member_ids == ["x", "y"]

    await repo.save(before, 0)

    assert await repo.get_team_by_id("t") == before
    (stored,) = backend.collections["teams_config"].docs
    assert "memberIds" not in stored and stored["member_ids"] == ["x", "y"]
    assert stored["version"] == 1


async def test_version_0_explicita_e_nula_se_comportam_como_legado(monkeypatch: pytest.MonkeyPatch) -> None:
    docs = [
        {**agent_document(_agent("zero")), "version": 0},
        {**agent_document(_agent("nula")), "version": None},
        {**agent_document(_agent("sem"))},
    ]
    repo = MongoBackend(monkeypatch).agents(docs)

    assert [(v.entity.id, v.version) for v in await repo.list_all()] == [("zero", 0), ("nula", 0), ("sem", 0)]
    assert [await repo.save(_agent(i, "novo"), 0) for i in ("zero", "nula", "sem")] == [1, 1, 1]
    await repo.delete("zero", 1)
    assert [(v.entity.id, v.version) for v in await repo.list_all()] == [("nula", 1), ("sem", 1)]


# ── Mongo: runtime x admin com versão inválida ──────────────────────


async def test_documento_com_version_invalida_segue_servido_ao_runtime_e_some_so_da_listagem(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = RecordingLogger()
    doc = {**agent_document(_agent()), "prompt": SECRET_MARKER, "version": "3"}
    repo = MongoBackend(monkeypatch).agents([doc], logger)

    assert [a.prompt for a in await repo.get_active_agents()] == [SECRET_MARKER]
    assert (await repo.get_agent_by_id("a")).prompt == SECRET_MARKER
    assert await repo.list_all() == []
    assert [r for r in logger.records if r.level == "error"]  # o documento isolado foi logado...
    assert all(SECRET_MARKER not in str(r) for r in logger.records)  # ...sem o conteúdo


async def test_save_em_documento_de_version_invalida_conflita_sem_alterar_o_documento(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    backend = MongoBackend(monkeypatch)
    repo = backend.agents([{**agent_document(_agent()), "version": "3"}])
    before = [dict(d) for d in backend.collections["agents_config"].docs]

    for expected in (0, 1, 3):
        with pytest.raises(ConfigVersionConflict):
            await repo.save(_agent("a", "x"), expected)

    assert backend.collections["agents_config"].docs == before


# ── Mongo: falha do store ───────────────────────────────────────────


class _BrokenWrites(FakeMongoCollection):
    async def replace_one(self, *_: object, **__: object) -> object:  # type: ignore[override]
        raise RuntimeError(f"mongo caiu {SECRET_MARKER}")

    async def delete_one(self, *_: object, **__: object) -> object:  # type: ignore[override]
        raise RuntimeError(f"mongo caiu {SECRET_MARKER}")

    def find(self, *_: object, **__: object) -> object:  # type: ignore[override]
        raise RuntimeError(f"mongo caiu {SECRET_MARKER}")


async def test_erro_do_store_propaga_como_esta_e_nao_vira_conflito(monkeypatch: pytest.MonkeyPatch) -> None:
    backend = MongoBackend(monkeypatch)
    logger = RecordingLogger()
    backend.agents([], logger)
    backend.collections["agents_config"] = _BrokenWrites()
    repo = MongoAgentConfigRepository(connection_string="mongodb://mongo.invalid", logger=logger)

    with pytest.raises(RuntimeError):
        await repo.save(_agent(), 0)
    with pytest.raises(RuntimeError):
        await repo.delete("a", 0)
    with pytest.raises(RuntimeError):
        await repo.list_all()

    errors = [r for r in logger.records if r.level == "error"]
    assert [r.message for r in errors] and all(SECRET_MARKER not in str(r) for r in logger.records)
    assert errors[-1].context.get("error_type") == "RuntimeError"


# ── YAML ────────────────────────────────────────────────────────────


def _tool(entity_id: str = "t") -> Tool:
    return Tool(
        id=entity_id, name="n", description="d", route="https://x.invalid", http_method=HttpMethod.GET, parameters=[]
    )


async def test_yaml_list_all_traz_a_version_do_arquivo_e_isola_a_invalida(tmp_path: Path) -> None:
    logger = RecordingLogger()
    docs = [
        {**agent_document(_agent("ok")), "version": 5},
        {**agent_document(_agent("ruim")), "version": "5"},
        {**agent_document(_agent("negativa")), "version": -2},
        {**agent_document(_agent("inativa")), "active": False},
    ]
    repo = YamlBackend(tmp_path).agents(docs, logger)

    listed = await repo.list_all()

    assert [(v.entity.id, v.version, v.entity.active) for v in listed] == [("ok", 5, True), ("inativa", 0, False)]
    assert [a.id for a in await repo.get_active_agents()] == ["ok", "ruim", "negativa"]
    assert {r.context.get("agent_id") for r in logger.records if r.level == "error"} == {"ruim", "negativa"}
