"""F3-06, ``live``: a migração das sessões do 2.5.8 contra um Mongo de verdade.

Só rodam com ``MONGO_LIVE_URL`` (marker ``live``; nunca na CI padrão); sem ela são pulados. Cada teste
usa um banco efêmero de nome único, apagado no fim (``tests/fakes/live_mongo.py``). As sessões são as
gravadas pelo agno 2.5.8 (``tests/fakes/legacy_sessions.py``); o modelo é o ``FakeChatModel``.

Cobrem o que o cliente em memória não prova: os filtros da migração v3.0.0 do agno (``$not``/``$size``)
e da conferência (agregação, ``index_information``) no servidor, o índice único de ``run_id`` real e o
``python -m`` com o db de sessões do ambiente. ``MONGO_LIVE_URL`` não entra em ``assert``, id de teste,
log nem ``repr``.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404: roda o próprio módulo do projeto (python -m), sem shell
import sys
from typing import Any

import pytest
from agno.agent import Agent
from agno.run.agent import RunOutput
from agno.run.base import RunStatus

from src.infrastructure.runtime.agno.session_db import RunOwnershipError, SessionDb
from src.infrastructure.runtime.agno.session_migration import SessionsReport, check, migrate, rollback
from tests.fakes import FakeChatModel
from tests.fakes.legacy_sessions import legacy_sessions, seed_legacy_sessions
from tests.fakes.live_mongo import LIVE_URL_ENV, LiveMongo
from tests.fakes.session_db import stored_runs

pytestmark = [pytest.mark.live, pytest.mark.usefixtures("live_native_closed")]

# Só a semente do id do db (o cliente vem pronto): nunca a URL real.
PLACEHOLDER_URL = "mongodb://mongo.live.invalid:27017"
MIGRATED = SessionsReport(
    schema_version="3.0.0",
    sessions_with_inline_runs=2,
    inline_runs=3,
    run_documents=3,
    inline_runs_not_in_runs=0,
    conflicting_runs=0,
    run_id_unique_index=True,
    duplicated_run_ids=0,
)


@pytest.fixture(autouse=True)
def _no_agno_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGNO_TELEMETRY", "false")


async def _seeded_db(live: LiveMongo, docs: list[dict[str, Any]] | None = None) -> SessionDb:
    """``SessionDb`` novo (``AsyncMongoClient`` novo) sobre o banco com as sessões gravadas pelo 2.5.8."""
    db = SessionDb(live.native(), db_url=PLACEHOLDER_URL, db_name=live.database)
    await seed_legacy_sessions(db.database[db.session_table_name], docs)
    return db


def _run_rows(live: LiveMongo) -> list[tuple[Any, ...]]:
    return sorted(
        (doc["run_id"], doc["session_id"], doc["user_id"], doc["run_index"], doc["run_data"]["content"])
        for doc in live.collection("agno_runs").find({}, {"_id": 0})
    )


def _sessions(live: LiveMongo) -> list[dict[str, Any]]:
    return list(live.collection("agno_sessions").find({}, {"_id": 0}).sort("session_id", 1))


def _run(run_id: str, session_id: str, user_id: str, content: str) -> RunOutput:
    return RunOutput(run_id=run_id, agent_id="ag", session_id=session_id, user_id=user_id, content=content)


async def test_live_migracao_copia_os_runs_do_2_5_8_sem_destruir_e_e_idempotente(live_mongo: LiveMongo) -> None:
    db = await _seeded_db(live_mongo)
    before = await check(db)

    first = await migrate(db)
    runs_after_first = list(live_mongo.collection("agno_runs").find({}).sort("run_id", 1))
    second = await migrate(db)

    assert (before.schema_version, before.inline_runs_not_in_runs, before.run_id_unique_index) == ("2.0.0", 3, False)
    assert first == second == MIGRATED
    assert list(live_mongo.collection("agno_runs").find({}).sort("run_id", 1)) == runs_after_first
    assert _run_rows(live_mongo) == [
        ("r-ana-1", "s-ana", "ana", 0, "O-CODIGO-DA-ANA-E-7421"),
        ("r-ana-2", "s-ana", "ana", 1, "anotado"),
        ("r-bruno-1", "s-bruno", "bruno", 0, "oi bruno"),
    ]
    assert _sessions(live_mongo) == legacy_sessions()


async def test_live_sessao_nao_migrada_continua_legivel(live_mongo: LiveMongo) -> None:
    db = await _seeded_db(live_mongo)

    before = [(run["run_id"], run["content"]) for run in await stored_runs(db, "s-ana", user_id="ana")]
    await migrate(db)
    after = [(run["run_id"], run["content"]) for run in await stored_runs(db, "s-ana", user_id="ana")]

    assert before == after == [("r-ana-1", "O-CODIGO-DA-ANA-E-7421"), ("r-ana-2", "anotado")]


async def test_live_conflito_de_run_id_legado_e_regenerate_do_dono_depois_da_migracao(live_mongo: LiveMongo) -> None:
    db = await _seeded_db(live_mongo)
    await db.upsert_run(_run("r-ana-1", "s-bruno", "bruno", "do bruno"), session_id="s-bruno", user_id="bruno")
    before = await check(db)

    after = await migrate(db)
    agent = Agent(id="ag", name="ag", model=FakeChatModel(responses=["regerada"]), db=db, telemetry=False)
    result = await agent.acontinue_run(run_id="r-ana-1", session_id="s-ana", user_id="ana", regenerate=True)

    assert (before.conflicting_runs, after.conflicting_runs, after.ok) == (1, 0, True)
    assert (result.status, result.content) == (RunStatus.completed, "regerada")
    original = live_mongo.collection("agno_runs").find_one({"run_id": "r-ana-1"})
    assert (original["session_id"], original["user_id"], original["status"]) == ("s-ana", "ana", "REGENERATED")
    with pytest.raises(RunOwnershipError):
        await db.upsert_run(_run("r-ana-1", "s-bruno", "bruno", "de novo"), session_id="s-bruno", user_id="bruno")


async def test_live_run_id_legado_em_duas_sessoes_e_run_id_repetido_sem_indice(live_mongo: LiveMongo) -> None:
    docs = legacy_sessions()
    next(doc for doc in docs if doc["session_id"] == "s-bruno")["runs"][0]["run_id"] = "r-ana-1"
    db = await _seeded_db(live_mongo, docs)

    migrated = await migrate(db)
    live_mongo.collection("agno_runs").drop()
    live_mongo.collection("agno_runs").insert_many([{"run_id": "r-x", "session_id": s} for s in ("s-a", "s-b")])
    without_index = await check(db)

    assert (migrated.run_documents, migrated.conflicting_runs, migrated.ok) == (2, 1, False)
    assert (without_index.run_id_unique_index, without_index.duplicated_run_ids) == (False, 1)


async def test_live_down_devolve_os_runs_a_sessao_e_a_proxima_gravacao_recria_o_indice(live_mongo: LiveMongo) -> None:
    db = await _seeded_db(live_mongo)
    await migrate(db)
    await db.upsert_run(_run("r-ana-3", "s-ana", "ana", "do 3.x"), session_id="s-ana", user_id="ana", run_index=2)

    report = await rollback(db)
    collections_after_down = await db.database.list_collection_names()
    ana = next(doc for doc in _sessions(live_mongo) if doc["session_id"] == "s-ana")
    await db.upsert_run(_run("r-ana-4", "s-ana", "ana", "depois"), session_id="s-ana", user_id="ana", run_index=3)

    assert "agno_runs" not in collections_after_down
    assert [run["run_id"] for run in ana["runs"]] == ["r-ana-1", "r-ana-2", "r-ana-3"]
    assert (report.schema_version, report.inline_runs, report.run_documents) == ("2.5.6", 4, 0)
    assert (await check(db)).run_id_unique_index
    assert (await migrate(db)).ok


def test_live_python_m_migra_e_imprime_a_conferencia(live_mongo: LiveMongo) -> None:
    sessions = live_mongo.collection("agno_sessions")
    sessions.insert_many(legacy_sessions())
    env = {
        **os.environ,
        "MONGO_CONNECTION_STRING": os.environ[LIVE_URL_ENV],
        "MONGO_DATABASE_NAME": live_mongo.database,
        "AGNO_TELEMETRY": "false",
    }
    module = "src.infrastructure.runtime.agno.session_migration"

    checked = subprocess.run(  # noqa: S603  # nosec B603: o próprio módulo pelo interpretador, sem shell
        [sys.executable, "-m", module, "--check"], env=env, capture_output=True, text=True, timeout=120, check=False
    )
    migrated = subprocess.run(  # noqa: S603  # nosec B603: o próprio módulo pelo interpretador, sem shell
        [sys.executable, "-m", module], env=env, capture_output=True, text=True, timeout=120, check=False
    )

    assert checked.returncode == 1
    assert "runs legados fora de agno_runs: 3" in checked.stdout
    assert migrated.returncode == 0
    assert migrated.stdout.splitlines()[-len(MIGRATED.lines()) :] == MIGRATED.lines()
    assert [doc["runs"] for doc in _sessions(live_mongo)] == [doc["runs"] for doc in legacy_sessions()]
