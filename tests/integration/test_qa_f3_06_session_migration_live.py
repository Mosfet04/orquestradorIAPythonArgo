"""QA do F3-06, ``live``: banco vazio e ``--check`` somente leitura, contra um Mongo de verdade.

Só rodam com ``MONGO_LIVE_URL`` (ver ``tests/fakes/live_mongo.py``); a URL não entra em ``assert`` nem log.
"""

from __future__ import annotations

import os
import subprocess  # nosec B404: roda o próprio módulo do projeto (python -m), sem shell
import sys

import pytest

from src.infrastructure.runtime.agno.session_db import SessionDb
from src.infrastructure.runtime.agno.session_migration import check, migrate
from tests.fakes.live_mongo import LIVE_URL_ENV, LiveMongo

pytestmark = [pytest.mark.live, pytest.mark.usefixtures("live_native_closed")]

PLACEHOLDER_URL = "mongodb://mongo.live.invalid:27017"


@pytest.fixture(autouse=True)
def _no_agno_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGNO_TELEMETRY", "false")


async def test_live_banco_novo_sem_sessoes_migra_e_a_conferencia_fica_ok(live_mongo: LiveMongo) -> None:
    """Deploy em ambiente sem sessões legadas: o índice único de ``run_id`` existe depois do ``up``."""
    db = SessionDb(live_mongo.native(), db_url=PLACEHOLDER_URL, db_name=live_mongo.database)

    report = await migrate(db)

    assert (report.inline_runs, report.run_documents, report.schema_version) == (0, 0, "3.0.0")
    assert report.run_id_unique_index, "banco novo ficou sem o índice único de run_id depois da migração"
    assert report.ok


async def test_live_conferir_banco_vazio_nao_cria_colecao_nem_levanta(live_mongo: LiveMongo) -> None:
    db = SessionDb(live_mongo.native(), db_url=PLACEHOLDER_URL, db_name=live_mongo.database)

    report = await check(db)

    assert not report.ok
    assert report.schema_version == "2.0.0"
    assert await db.database.list_collection_names() == []


def test_live_python_m_check_em_banco_vazio_sai_com_1_sem_criar_colecao(live_mongo: LiveMongo) -> None:
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

    assert checked.returncode == 1
    assert "migração pendente" in checked.stdout
    assert live_mongo.sync()[live_mongo.database].list_collection_names() == []
