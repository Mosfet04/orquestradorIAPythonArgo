"""F3-06: migração das sessões do 2.5.8 (``runs`` inline) para ``agno_runs`` e a conferência dela.

Roda o ``MigrationManager`` do agno 3.1.12 de verdade sobre o ``SessionDb`` com o cliente nativo em
memória (``tests/fakes/mongo.py``), com os documentos gravados pelo 2.5.8 (``tests/fakes/legacy_sessions.py``).
"""

from __future__ import annotations

import copy
import inspect
from typing import Any

import pytest
from agno.run.agent import RunOutput
from pymongo.errors import ServerSelectionTimeoutError

from src.infrastructure.runtime.agno import session_migration
from src.infrastructure.runtime.agno.session_db import RunOwnershipError, SessionDb
from src.infrastructure.runtime.agno.session_migration import SessionsReport, check, migrate, rollback
from tests.fakes.legacy_sessions import LEGACY_RUNS, legacy_sessions, seed_legacy_sessions
from tests.fakes.mongo import FakeAsyncMongoClient
from tests.fakes.session_db import collection, fake_session_db, session_db_client, session_docs, stored_runs

# Texto de teste no formato de um erro de driver com URI (montado em partes: não é credencial).
DRIVER_TEXT = "sem rota: mongodb://" + "usuario:" + "SENHA-DO-MONGO" + "@mongo.interno:27017"

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


@pytest.fixture
async def db() -> SessionDb:
    """``SessionDb`` com as sessões gravadas pelo 2.5.8 e nada mais."""
    session_db = fake_session_db()
    await seed_legacy_sessions(collection(session_db, session_db.session_table_name))
    return session_db


def _run_rows(db: SessionDb) -> list[tuple[Any, ...]]:
    """``(run_id, session_id, user_id, run_index, conteúdo)`` de cada documento de ``agno_runs``."""
    docs = collection(db, db.runs_table_name).docs
    return sorted(
        (doc["run_id"], doc["session_id"], doc["user_id"], doc.get("run_index"), doc["run_data"].get("content"))
        for doc in docs
    )


def _without_ids(docs: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{key: value for key, value in doc.items() if key != "_id"} for doc in docs]


def _run(run_id: str, session_id: str, user_id: str, content: str) -> RunOutput:
    return RunOutput(run_id=run_id, agent_id="ag", session_id=session_id, user_id=user_id, content=content)


def _collections(db: SessionDb) -> list[str]:
    database = session_db_client(db)[db.db_name]
    return sorted(name for name, coll in database.collections.items() if coll.docs or coll.indexes)


# ── migração ─────────────────────────────────────────────────────────────────────────────────────


async def test_migrar_copia_os_runs_legados_para_agno_runs_com_a_sessao_e_o_dono(db: SessionDb) -> None:
    report = await migrate(db)

    assert report == MIGRATED
    assert report.ok
    assert _run_rows(db) == [
        ("r-ana-1", "s-ana", "ana", 0, "O-CODIGO-DA-ANA-E-7421"),
        ("r-ana-2", "s-ana", "ana", 1, "anotado"),
        ("r-bruno-1", "s-bruno", "bruno", 0, "oi bruno"),
    ]
    # Não destrói: o campo legado ``runs`` segue como estava (cleanup é manual, release posterior).
    assert _without_ids(session_docs(db)) == legacy_sessions()


async def test_conferir_sem_migrar_mostra_o_que_falta_e_nao_grava_nada(db: SessionDb) -> None:
    report = await check(db)

    assert report == SessionsReport(
        schema_version="2.0.0",
        sessions_with_inline_runs=2,
        inline_runs=3,
        run_documents=0,
        inline_runs_not_in_runs=3,
        conflicting_runs=0,
        run_id_unique_index=False,
        duplicated_run_ids=0,
    )
    assert not report.ok
    assert _collections(db) == [db.session_table_name]


async def test_migrar_duas_vezes_da_o_mesmo_resultado(db: SessionDb) -> None:
    first = await migrate(db)
    runs_after_first = copy.deepcopy(collection(db, db.runs_table_name).docs)
    sessions_after_first = copy.deepcopy(session_docs(db))

    second = await migrate(db)

    assert second == first == MIGRATED
    assert collection(db, db.runs_table_name).docs == runs_after_first
    assert session_docs(db) == sessions_after_first


async def test_sessao_nao_migrada_continua_legivel_e_le_o_mesmo_depois_da_migracao(db: SessionDb) -> None:
    def contents(runs: list[dict[str, Any]]) -> list[tuple[str, str]]:
        return [(run["run_id"], run["content"]) for run in runs]

    before = contents(await stored_runs(db, "s-ana", user_id="ana"))
    await migrate(db)
    after = contents(await stored_runs(db, "s-ana", user_id="ana"))

    assert before == after == [("r-ana-1", "O-CODIGO-DA-ANA-E-7421"), ("r-ana-2", "anotado")]


# ── conflitos de run_id (achado R1 do F3-05) e índice único (achado N1) ─────────────────────────


async def test_run_id_legado_gravado_antes_por_outro_dono_e_conflito_e_a_migracao_o_devolve_ao_dono(
    db: SessionDb,
) -> None:
    # Antes da migração o run legado não está em ``agno_runs``: o Bruno grava um run com o id dele.
    await db.upsert_run(_run("r-ana-1", "s-intruso", "bruno", "do bruno"), session_id="s-intruso", user_id="bruno")
    before = await check(db)

    after = await migrate(db)

    assert (before.conflicting_runs, before.inline_runs_not_in_runs, before.ok) == (1, 2, False)
    assert after == SessionsReport(**{**MIGRATED.__dict__, "run_documents": 3})
    assert ("r-ana-1", "s-ana", "ana", 0, "O-CODIGO-DA-ANA-E-7421") in _run_rows(db)
    with pytest.raises(RunOwnershipError):
        await db.upsert_run(_run("r-ana-1", "s-intruso", "bruno", "de novo"), session_id="s-intruso", user_id="bruno")


async def test_run_id_legado_repetido_em_duas_sessoes_fica_como_conflito_a_tratar_a_mao() -> None:
    db = fake_session_db()
    docs = legacy_sessions()
    bruno = next(doc for doc in docs if doc["session_id"] == "s-bruno")
    bruno["runs"][0]["run_id"] = "r-ana-1"  # o 2.5.8 aceitava o mesmo run_id em sessões diferentes
    await seed_legacy_sessions(collection(db, db.session_table_name), docs)

    report = await migrate(db)

    assert (report.inline_runs, report.run_documents, report.conflicting_runs) == (3, 2, 1)
    assert not report.ok
    assert any("conflito" in line and "à mão" in line for line in report.lines())


async def test_sem_o_indice_unico_de_run_id_a_conferencia_conta_o_run_id_repetido(db: SessionDb) -> None:
    runs = collection(db, db.runs_table_name)
    for session_id in ("s-a", "s-b"):
        await runs.insert_one({"run_id": "r-x", "session_id": session_id, "user_id": None, "run_data": {}})

    report = await check(db)

    assert (report.run_id_unique_index, report.duplicated_run_ids, report.ok) == (False, 1, False)


# ── rollback por down ───────────────────────────────────────────────────────────────────────────


async def test_down_devolve_os_runs_ao_campo_da_sessao_e_apaga_agno_runs(db: SessionDb) -> None:
    await migrate(db)
    # Run gravado pelo 3.x depois da migração: só existe em ``agno_runs`` até o down.
    await db.upsert_run(_run("r-ana-3", "s-ana", "ana", "do 3.x"), session_id="s-ana", user_id="ana", run_index=2)

    report = await rollback(db)

    assert db.runs_table_name not in _collections(db)
    ana = next(doc for doc in session_docs(db) if doc["session_id"] == "s-ana")
    assert [(run["run_id"], run["content"]) for run in ana["runs"]] == [
        ("r-ana-1", "O-CODIGO-DA-ANA-E-7421"),
        ("r-ana-2", "anotado"),
        ("r-ana-3", "do 3.x"),
    ]
    assert (report.schema_version, report.inline_runs, report.run_documents) == ("2.5.6", 4, 0)
    assert await migrate(db) == SessionsReport(**{**MIGRATED.__dict__, "inline_runs": 4, "run_documents": 4})


async def test_depois_do_down_a_proxima_gravacao_recria_o_indice_unico_de_run_id(db: SessionDb) -> None:
    await migrate(db)
    await rollback(db)

    await db.upsert_run(_run("r-ana-3", "s-ana", "ana", "do 3.x"), session_id="s-ana", user_id="ana")

    assert (await check(db)).run_id_unique_index
    with pytest.raises(RunOwnershipError):
        await db.upsert_run(_run("r-ana-3", "s-intruso", "bruno", "do bruno"), session_id="s-intruso", user_id="bruno")


# ── módulo executável ───────────────────────────────────────────────────────────────────────────


def test_modulo_nao_referencia_o_cleanup_do_campo_legado() -> None:
    assert "cleanup_legacy_runs" not in inspect.getsource(session_migration)


def _open_with(monkeypatch: pytest.MonkeyPatch, db: SessionDb) -> None:
    """O ``python -m`` abre este db de sessões (cliente em memória) no lugar do da URL do ambiente."""
    monkeypatch.setattr(session_migration, "create_session_db", lambda db_url, db_name: db)


async def _seeded_db(client: FakeAsyncMongoClient | None = None) -> SessionDb:
    db = fake_session_db(client)
    await seed_legacy_sessions(collection(db, db.session_table_name))
    return db


async def test_python_m_migra_imprime_a_conferencia_e_fecha_o_db(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    client = FakeAsyncMongoClient()
    db = await _seeded_db(client)
    _open_with(monkeypatch, db)

    async def refuse_cleanup(self: SessionDb, force: bool = False) -> bool:
        raise AssertionError("a migração não pode chamar o cleanup do campo legado")

    monkeypatch.setattr(SessionDb, "cleanup_legacy_runs_field", refuse_cleanup)

    code = await session_migration.run([])

    assert code == 0
    assert capsys.readouterr().out.splitlines() == MIGRATED.lines()
    assert client.closes, "o db de sessões não foi fechado"
    assert _without_ids(session_docs(db)) == legacy_sessions()


async def test_python_m_check_so_confere_e_sai_com_1_quando_ha_pendencia(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seeded = await _seeded_db()
    expected = (await check(seeded)).lines()
    _open_with(monkeypatch, seeded)

    code = await session_migration.run(["--check"])

    out = capsys.readouterr().out
    assert code == 1
    assert out.splitlines() == expected
    assert "runs legados fora de agno_runs: 3" in out
    assert collection(seeded, seeded.runs_table_name).docs == []


async def test_python_m_down_volta_o_esquema_e_sai_com_0(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    seeded = await _seeded_db()
    await migrate(seeded)
    _open_with(monkeypatch, seeded)

    code = await session_migration.run(["--down"])

    assert code == 0
    assert "versão do esquema de sessões: 2.5.6" in capsys.readouterr().out
    assert seeded.runs_table_name not in _collections(seeded)


async def test_python_m_com_mongo_fora_sai_com_2_sem_o_texto_do_driver(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    class DownAsyncMongoClient(FakeAsyncMongoClient):
        def refuse_if_closed(self) -> None:
            raise ServerSelectionTimeoutError(DRIVER_TEXT)

    client = DownAsyncMongoClient()
    _open_with(monkeypatch, fake_session_db(client))

    code = await session_migration.run([])

    captured = capsys.readouterr()
    assert code == 2
    assert "ServerSelectionTimeoutError" in captured.err
    assert "SENHA-DO-MONGO" not in captured.err + captured.out
    assert client.closes, "o db de sessões não foi fechado"


@pytest.mark.parametrize("where", ["AppConfig.load", "create_session_db", "migrate"])
async def test_python_m_com_falha_fora_do_mongo_sai_com_2_e_so_o_tipo_do_erro(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], where: str
) -> None:
    """Falha que não é do Mongo também sai com 2 (1 é pendência), sem traceback nem o texto do erro."""
    client = FakeAsyncMongoClient()
    db = await _seeded_db(client)
    _open_with(monkeypatch, db)

    def fail(*_: object) -> Any:
        raise RuntimeError(DRIVER_TEXT)

    if where == "AppConfig.load":
        monkeypatch.setattr(session_migration.AppConfig, "load", fail)
    elif where == "create_session_db":
        monkeypatch.setattr(session_migration, "create_session_db", fail)
    else:
        monkeypatch.setattr(session_migration, "MigrationManager", fail)

    code = await session_migration.run([])

    captured = capsys.readouterr()
    assert code == 2
    assert "RuntimeError" in captured.err
    assert "SENHA-DO-MONGO" not in captured.err + captured.out
    assert captured.out == ""
    assert bool(client.closes) is (where == "migrate"), "o db aberto tem de ser fechado (e só ele)"


async def test_sem_o_carimbo_3_0_0_a_conferencia_tem_pendencia_mesmo_com_agno_runs_completa(db: SessionDb) -> None:
    """Achado R4: runs copiados e índice criado sem a migração rodar não dão ok (o carimbo falta)."""
    await migrate(db)
    await collection(db, db.versions_table_name).delete_many({})

    report = await check(db)

    assert report == SessionsReport(**{**MIGRATED.__dict__, "schema_version": "2.0.0"})
    assert not report.ok
    assert "versão do esquema de sessões: 2.0.0 (abaixo de 3.0.0: migração pendente)" in report.lines()
    assert report.lines()[-1] == "conferência: há pendência"


async def test_python_m_down_sai_com_0_e_mostra_a_migracao_pendente(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """Depois do rollback o esquema fica abaixo de 3.0.0: a conferência acusa, mas o ``--down`` foi feito."""
    seeded = await _seeded_db()
    await migrate(seeded)
    _open_with(monkeypatch, seeded)

    code = await session_migration.run(["--down"])

    lines = capsys.readouterr().out.splitlines()
    assert code == 0
    assert lines[0] == "versão do esquema de sessões: 2.5.6 (abaixo de 3.0.0: migração pendente)"
    assert lines[-1] == "conferência: há pendência"


async def test_conferencia_le_das_sessoes_so_o_run_id_dos_runs_legados(
    db: SessionDb, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Achado N1: a conferência não traz o conteúdo dos runs (projeção ``runs.run_id``)."""
    sessions = collection(db, db.session_table_name)
    read: list[dict[str, Any]] = []
    original_find = sessions.find

    def spy(query: dict[str, Any], projection: dict[str, Any] | None = None, **kwargs: Any) -> Any:
        cursor = original_find(query, projection, **kwargs)
        read.extend(cursor._items)
        return cursor

    monkeypatch.setattr(sessions, "find", spy)

    report = await check(db)

    assert (report.inline_runs, report.inline_runs_not_in_runs) == (3, 3)
    assert sorted((doc["session_id"], doc["runs"]) for doc in read) == [
        ("s-ana", [{"run_id": "r-ana-1"}, {"run_id": "r-ana-2"}]),
        ("s-bruno", [{"run_id": "r-bruno-1"}]),
    ]


def test_legacy_runs_da_fixture_batem_com_os_documentos() -> None:
    """Guarda da fixture: os runs descritos em ``LEGACY_RUNS`` são os do JSON gerado pelo 2.5.8."""
    found = {
        run["run_id"]: (doc["session_id"], doc["user_id"], run["content"])
        for doc in legacy_sessions()
        for run in doc["runs"]
    }
    assert found == LEGACY_RUNS
