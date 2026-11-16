"""F3-06 pelo app e pelo agente: a rota de migração do AgentOS e o ``regenerate`` do dono depois dela.

Sessões gravadas pelo agno 2.5.8 (``tests/fakes/legacy_sessions.py``) no ``SessionDb`` com o cliente
motor em memória; o agente e o AgentOS são os do agno 3.1.12, com modelo roteirizado.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from agno.agent import Agent
from agno.run.agent import RunOutput
from agno.run.base import RunStatus
from fastapi import FastAPI

from src.infrastructure.runtime.agno.session_db import SessionDb
from src.infrastructure.runtime.agno.session_migration import check, migrate
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import FakeChatModel
from tests.fakes.legacy_sessions import seed_legacy_sessions
from tests.fakes.session_db import collection, fake_session_db, session_db_client, session_docs
from tests.fakes.web import mount_agent_os

RUN_KEY = "f306-run-key-" + "r" * 20
ADMIN_KEY = "f306-admin-key-" + "a" * 20
_ENV_NAMES = ("ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS", "APP_HOST", "API_KEY_RUN", "API_KEY_ADMIN")


@dataclass
class World:
    app: FastAPI
    db: SessionDb
    agent: Agent

    def client(self, key: str) -> httpx.AsyncClient:
        transport = httpx.ASGITransport(app=self.app, raise_app_exceptions=False)
        return httpx.AsyncClient(
            transport=transport, base_url="http://test", headers={"Authorization": f"Bearer {key}"}
        )


@pytest.fixture
async def world(monkeypatch: pytest.MonkeyPatch) -> World:
    """App de produção com o agente ``ag`` (o das sessões do 2.5.8) e as sessões legadas no db."""
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("API_KEY_RUN", RUN_KEY)
    monkeypatch.setenv("API_KEY_ADMIN", ADMIN_KEY)
    db = fake_session_db()
    await seed_legacy_sessions(collection(db, db.session_table_name))
    model = FakeChatModel(responses=["resposta regerada"])
    agent = Agent(id="ag", name="ag", model=model, db=db, telemetry=False, add_history_to_context=True)
    factory = AppFactory()
    app = factory.create_app()
    mount_agent_os(factory, app, [agent], [], session_db=db)
    return World(app, db, agent)


@pytest.mark.parametrize("db_path", ["{db_id}", "all"], ids=["db-de-sessoes", "todos"])
async def test_rota_de_migracao_responde_200_e_migra_as_sessoes(world: World, db_path: str) -> None:
    async with world.client(ADMIN_KEY) as client:
        response = await client.post(f"/databases/{db_path.format(db_id=world.db.id)}/migrate")

    assert response.status_code == 200, response.text
    report = await check(world.db)
    assert (report.schema_version, report.run_documents, report.ok) == ("3.0.0", 3, True)


async def test_rota_de_migracao_segue_so_para_a_chave_admin(world: World) -> None:
    async with world.client(RUN_KEY) as client:
        response = await client.post(f"/databases/{world.db.id}/migrate")

    assert response.status_code == 403
    assert (await check(world.db)).run_documents == 0


async def test_rota_de_migracao_com_target_version_menor_faz_o_down_no_processo_vivo(world: World) -> None:
    """Achado R1 da revisão: a rota do agno (só admin) desfaz a v3.0.0 e apaga ``agno_runs`` (runbook F3-07)."""
    await migrate(world.db)
    run_3x = RunOutput(run_id="r-ana-3", agent_id="ag", session_id="s-ana", user_id="ana", content="do 3.x")
    await world.db.upsert_run(run_3x, session_id="s-ana", user_id="ana", run_index=2)

    async with world.client(ADMIN_KEY) as client:
        response = await client.post(f"/databases/{world.db.id}/migrate", params={"target_version": "2.5.6"})

    assert response.status_code == 200, response.text
    ana = next(doc for doc in session_docs(world.db) if doc["session_id"] == "s-ana")
    assert [run["run_id"] for run in ana["runs"]] == ["r-ana-1", "r-ana-2", "r-ana-3"]
    runs = session_db_client(world.db)[world.db.db_name].collections.get(world.db.runs_table_name)
    assert runs is None or (runs.docs, runs.indexes) == ([], [])
    report = await check(world.db)
    assert (report.schema_version, report.run_documents, report.ok) == ("2.5.6", 0, False)


async def _intruder_takes_the_legacy_run_id(db: SessionDb) -> None:
    """O Bruno grava, na sessão dele, um run com o ``run_id`` do run legado da Ana (achado R1 do F3-05)."""
    intruder = RunOutput(run_id="r-ana-1", agent_id="ag", session_id="s-bruno", user_id="bruno", content="do bruno")
    await db.upsert_run(intruder, session_id="s-bruno", user_id="bruno")


def _run_doc(db: SessionDb, run_id: str) -> dict[str, Any]:
    (doc,) = [doc for doc in collection(db, db.runs_table_name).docs if doc["run_id"] == run_id]
    return doc


async def test_regenerate_do_dono_sobre_run_legado_funciona_depois_da_migracao(world: World) -> None:
    await _intruder_takes_the_legacy_run_id(world.db)
    await migrate(world.db)

    result = await world.agent.acontinue_run(run_id="r-ana-1", session_id="s-ana", user_id="ana", regenerate=True)

    assert (result.status, result.content) == (RunStatus.completed, "resposta regerada")
    original = _run_doc(world.db, "r-ana-1")
    assert (original["session_id"], original["user_id"], original["status"]) == ("s-ana", "ana", "REGENERATED")
    regenerated = _run_doc(world.db, result.run_id)
    assert (regenerated["session_id"], regenerated["user_id"]) == ("s-ana", "ana")


async def test_sem_a_migracao_o_regenerate_do_dono_nao_marca_o_run_legado(world: World) -> None:
    """Controle: a janela do R1 existe antes da migração (o run do Bruno fica com o ``run_id``)."""
    await _intruder_takes_the_legacy_run_id(world.db)

    await world.agent.acontinue_run(run_id="r-ana-1", session_id="s-ana", user_id="ana", regenerate=True)

    taken = _run_doc(world.db, "r-ana-1")
    assert (taken["session_id"], taken["user_id"], taken["run_data"]["content"]) == ("s-bruno", "bruno", "do bruno")
    assert taken["status"] != "REGENERATED"
