"""QA do F3-06: bordas da migração e da conferência que o dev não cobriu (documentos legados hostis)."""

from __future__ import annotations

from typing import Any

import pytest

from src.infrastructure.runtime.agno import session_migration
from src.infrastructure.runtime.agno.session_migration import check, migrate
from tests.fakes.legacy_sessions import legacy_sessions, seed_legacy_sessions
from tests.fakes.session_db import collection, fake_session_db


async def test_banco_sem_nenhuma_sessao_migra_e_a_conferencia_fica_ok() -> None:
    db = fake_session_db()

    report = await migrate(db)

    assert report.ok
    assert (report.inline_runs, report.run_documents, report.schema_version) == (0, 0, "3.0.0")


async def test_documentos_legados_atipicos_nao_quebram_a_migracao_nem_a_conferencia() -> None:
    """``runs`` vazio, nulo, ausente, sem ``run_id`` e sessão sem ``user_id``: a conferência bate com a cópia."""
    db = fake_session_db()
    await seed_legacy_sessions(
        collection(db, db.session_table_name),
        [
            {"session_id": "s1", "user_id": None, "session_type": "agent", "runs": [{"run_id": "a", "content": "x"}]},
            {"session_id": "s2", "user_id": "u", "session_type": "agent", "runs": []},
            {"session_id": "s3", "user_id": "u", "session_type": "agent", "runs": None},
            {"session_id": "s4", "user_id": "u", "session_type": "agent"},
        ],
    )

    before = await check(db)
    after = await migrate(db)

    assert (before.sessions_with_inline_runs, before.inline_runs, before.inline_runs_not_in_runs) == (1, 1, 1)
    assert after.ok
    assert (after.sessions_with_inline_runs, after.inline_runs, after.run_documents) == (1, 1, 1)


@pytest.mark.parametrize(
    ("squatter_session", "squatter_user"),
    [("s-ana", "bruno"), ("s-outra", "ana"), ("s-outra", "bruno")],
    ids=["mesma-sessao-outro-user", "outra-sessao-mesmo-user", "outra-sessao-outro-user"],
)
async def test_conferencia_conta_conflito_por_sessao_e_por_user_id_separadamente(
    squatter_session: str, squatter_user: str
) -> None:
    db = fake_session_db()
    await seed_legacy_sessions(collection(db, db.session_table_name))
    await collection(db, db.runs_table_name).insert_one(
        {"run_id": "r-ana-1", "session_id": squatter_session, "user_id": squatter_user, "run_data": {}}
    )

    report = await check(db)

    assert (report.conflicting_runs, report.inline_runs_not_in_runs, report.ok) == (1, 2, False)


async def test_run_legado_ja_copiado_com_o_dono_certo_nao_e_conflito() -> None:
    db = fake_session_db()
    await seed_legacy_sessions(collection(db, db.session_table_name))
    await collection(db, db.runs_table_name).insert_one(
        {"run_id": "r-ana-1", "session_id": "s-ana", "user_id": "ana", "run_data": {}}
    )

    report = await check(db)

    assert (report.conflicting_runs, report.inline_runs_not_in_runs) == (0, 2)


async def test_sessao_sem_user_id_e_run_copiado_sem_user_id_tem_o_mesmo_dono() -> None:
    """Sessão sem ``user_id`` e run copiado sem ``user_id``: o mesmo dono, sem conflito."""
    docs: list[dict[str, Any]] = legacy_sessions()
    for doc in docs:
        doc["user_id"] = None
    db = fake_session_db()
    await seed_legacy_sessions(collection(db, db.session_table_name), docs)

    assert (await migrate(db)).ok


@pytest.mark.parametrize("argv", [["--check", "--down"], ["--bogus"]])
async def test_python_m_com_argumentos_invalidos_nao_toca_no_banco(
    argv: list[str], monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    opened: list[object] = []
    monkeypatch.setattr(session_migration, "create_session_db", lambda *_: opened.append(1))

    with pytest.raises(SystemExit) as info:
        await session_migration.run(argv)

    assert info.value.code == 2
    assert not opened
    assert capsys.readouterr().out == ""
