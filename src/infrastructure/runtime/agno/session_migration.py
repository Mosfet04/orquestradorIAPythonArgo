"""Migração das sessões para o esquema do agno 3.x (F3-06), executável por ``python -m``.

O agno 3.x guarda cada run num documento de ``agno_runs`` (chave ``run_id``, índice único); o 2.5.8 os
guardava no campo ``runs`` do documento da sessão. A migração v3.0.0 do agno 3.1.12
(``MigrationManager(db).up()``, ``agno/db/migrations/versions/v3_0_0.py``) copia cada run do campo
legado para ``agno_runs`` com a ``session_id`` e o ``user_id`` da sessão dona (``replace_one`` com
``upsert`` por ``run_id``), carimba a versão em ``agno_schema_versions`` e não apaga o campo legado: o
``get_session`` do 3.x junta os dois. Apagar o campo é manual, num release posterior; este módulo não
o faz. O ``down`` (``MigrationManager(db).down``) devolve os runs de ``agno_runs`` ao campo da sessão
e apaga ``agno_runs``.

Uso, com o ambiente do app (``MONGO_CONNECTION_STRING``, ``MONGO_DATABASE_NAME``)::

    python -m src.infrastructure.runtime.agno.session_migration           # migra e confere
    python -m src.infrastructure.runtime.agno.session_migration --check   # só confere
    python -m src.infrastructure.runtime.agno.session_migration --down    # volta ao esquema 2.5.6

Saída 0: ok (ou ``--down`` feito); 1: a conferência achou pendência; 2: falha (do Mongo ou outra),
só com o tipo do erro.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from agno.db.migrations.manager import MigrationManager
from packaging.version import Version
from pymongo.errors import PyMongoError

from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.runtime.agno.session_db import SessionDb, create_session_db

SCHEMA_VERSION = "3.0.0"
"""Versão do esquema de sessões que a conferência exige: a da migração que cria ``agno_runs``."""
ROLLBACK_VERSION = "2.5.6"
"""Versão anterior à 3.0.0 na lista do ``MigrationManager``: o ``down`` até ela desfaz só a v3.0.0."""

# Sessão com runs no campo legado: o mesmo filtro da migração v3.0.0 do agno.
_INLINE_RUNS = {"runs": {"$exists": True, "$ne": None, "$not": {"$size": 0}}}
_DUPLICATED_RUN_IDS: list[dict[str, Any]] = [
    {"$match": {"run_id": {"$ne": None}}},
    {"$group": {"_id": "$run_id", "count": {"$sum": 1}}},
    {"$match": {"count": {"$gt": 1}}},
    {"$count": "duplicated"},
]


@dataclass(frozen=True)
class SessionsReport:
    """Conferência das coleções de sessões e de runs.

    ``conflicting_runs``: run do campo legado cujo ``run_id`` está em ``agno_runs`` com outra sessão
    ou outro ``user_id``. Antes da migração é o run que alguém gravou com o ``run_id`` de um run
    legado (a migração o troca pelo do dono); depois dela, o ``run_id`` repetido em duas sessões
    legadas (fica com a última copiada): conflito a tratar à mão.
    """

    schema_version: str
    sessions_with_inline_runs: int
    inline_runs: int
    run_documents: int
    inline_runs_not_in_runs: int
    conflicting_runs: int
    run_id_unique_index: bool
    duplicated_run_ids: int

    @property
    def ok(self) -> bool:
        """Esquema carimbado em 3.0.0+, todo run legado em ``agno_runs`` com o dono e ``run_id`` único."""
        return (
            self.schema_migrated
            and self.inline_runs_not_in_runs == 0
            and self.conflicting_runs == 0
            and self.run_id_unique_index
            and self.duplicated_run_ids == 0
        )

    @property
    def schema_migrated(self) -> bool:
        """O ``MigrationManager`` carimbou o esquema de sessões em ``SCHEMA_VERSION`` ou acima."""
        return Version(self.schema_version) >= Version(SCHEMA_VERSION)

    def lines(self) -> list[str]:
        """Linhas da conferência, como o ``python -m`` as imprime."""
        pending = "" if self.schema_migrated else f" (abaixo de {SCHEMA_VERSION}: migração pendente)"
        return [
            f"versão do esquema de sessões: {self.schema_version}{pending}",
            f"sessões com runs no campo legado: {self.sessions_with_inline_runs}",
            f"runs no campo legado: {self.inline_runs}",
            f"documentos em agno_runs: {self.run_documents}",
            f"runs legados fora de agno_runs: {self.inline_runs_not_in_runs}",
            "runs legados em agno_runs com outra sessão ou outro user_id (conflito a tratar à mão): "
            f"{self.conflicting_runs}",
            f"índice único de run_id em agno_runs: {'sim' if self.run_id_unique_index else 'não'}",
            f"run_id repetido em agno_runs: {self.duplicated_run_ids}",
            "conferência: ok" if self.ok else "conferência: há pendência",
        ]


async def check(db: SessionDb) -> SessionsReport:
    """Confere as coleções sem gravar nada (nem cria coleção ou índice)."""
    sessions = db.database[db.session_table_name]
    runs = db.database[db.runs_table_name]
    with_inline = inline = not_in_runs = conflicting = 0
    async for session in sessions.find(_INLINE_RUNS, {"session_id": 1, "user_id": 1, "runs.run_id": 1}):
        owner = (session.get("session_id"), session.get("user_id"))
        run_ids = _legacy_run_ids(session.get("runs"))
        with_inline += 1
        inline += len(run_ids)
        stored: dict[str, list[tuple[Any, Any]]] = {}
        async for doc in runs.find({"run_id": {"$in": run_ids}}, {"run_id": 1, "session_id": 1, "user_id": 1}):
            stored.setdefault(doc["run_id"], []).append((doc.get("session_id"), doc.get("user_id")))
        for run_id in run_ids:
            if run_id not in stored:
                not_in_runs += 1
            elif any(found != owner for found in stored[run_id]):
                conflicting += 1
    duplicated = await db._aggregate_to_list(runs, _DUPLICATED_RUN_IDS)
    version = await db.get_latest_schema_version(db.session_table_name)
    return SessionsReport(
        # Sem carimbo o agno já devolve "2.0.0" (``AsyncMongoDb.get_latest_schema_version``); o ``or`` só
        # cobre o ``Optional`` da assinatura, com o mesmo default.
        schema_version=version or "2.0.0",
        sessions_with_inline_runs=with_inline,
        inline_runs=inline,
        run_documents=await runs.count_documents({}),
        inline_runs_not_in_runs=not_in_runs,
        conflicting_runs=conflicting,
        run_id_unique_index=_has_unique_run_id_index(await runs.index_information()),
        duplicated_run_ids=duplicated[0]["duplicated"] if duplicated else 0,
    )


def _legacy_run_ids(runs: object) -> list[str]:
    """``run_id`` dos runs do campo legado que a migração copia (dict com ``run_id``)."""
    if not isinstance(runs, list):
        return []
    return [run["run_id"] for run in runs if isinstance(run, dict) and run.get("run_id") is not None]


def _has_unique_run_id_index(indexes: Mapping[str, Any]) -> bool:
    return any(spec.get("unique") is True and list(spec.get("key", [])) == [("run_id", 1)] for spec in indexes.values())


async def migrate(db: SessionDb) -> SessionsReport:
    """Migra todas as coleções do db até a última versão do agno (v3.0.0) e confere."""
    await MigrationManager(db).up()
    return await check(db)


async def rollback(db: SessionDb) -> SessionsReport:
    """Desfaz a v3.0.0 (runs de volta ao campo da sessão, ``agno_runs`` apagada) e confere."""
    await MigrationManager(db).down(ROLLBACK_VERSION)
    return await check(db)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m src.infrastructure.runtime.agno.session_migration",
        description="Migra as sessões do agno 2.5.8 (runs no campo da sessão) para agno_runs e confere.",
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--check", action="store_true", help="só confere, sem migrar")
    mode.add_argument("--down", action="store_true", help=f"desfaz a v3.0.0 (esquema {ROLLBACK_VERSION})")
    return parser


async def run(argv: Sequence[str]) -> int:
    """Corpo do ``python -m``: abre o db de sessões do ambiente, executa, imprime e fecha."""
    args = _parser().parse_args(argv)
    db: SessionDb | None = None
    try:
        config = AppConfig.load()
        db = create_session_db(config.mongo_connection_string, config.mongo_database_name)
        if args.check:
            report = await check(db)
        elif args.down:
            report = await rollback(db)
        else:
            report = await migrate(db)
        output, ok = report.lines(), report.ok
    except PyMongoError as exc:
        # Só o tipo: o texto do erro do driver pode trazer a URI do Mongo.
        sys.stderr.write(f"Falha no Mongo ({type(exc).__name__}): operação interrompida; confira com --check.\n")
        return 2
    except Exception as exc:  # noqa: BLE001 - borda do CLI: falha vira saída 2 (1 é pendência), sem traceback
        # Só o tipo: o texto da exceção (config, driver) pode trazer a URI do Mongo.
        sys.stderr.write(f"Falha na migração ({type(exc).__name__}): operação interrompida; confira com --check.\n")
        return 2
    finally:
        if db is not None:
            await db.close()
    sys.stdout.write("\n".join(output) + "\n")
    return 0 if args.down or ok else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(sys.argv[1:])))
