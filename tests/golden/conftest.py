"""Infra dos golden tests: snapshot JSON versionado + espião dos construtores do agno.

O espião troca ``__init__`` nas CLASSES ``agno.agent.Agent`` e ``agno.team.Team`` (não no
módulo que as importa). Assim o golden continua valendo quando a montagem sair de
``src/application/services`` para o adapter do runtime (F2): quem quer que construa o
``Agent``/``Team``, a chamada passa pelo espião.

I/O real é cortado na borda do agno, de forma estável:
- ``agno.db.mongo.mongo.MongoClient`` cria o cliente com ``connect=False`` (nada abre socket);
- ``MongoVectorDb.exists`` responde ``True`` (``Knowledge`` não tenta criar coleção/índice);
- ``Knowledge.insert`` só registra a chamada (sem leitura de arquivo, embedding ou escrita).

Snapshots: ``tests/golden/snapshots/<nome>.json``. Só são regravados com ``--update-golden``.
"""

from __future__ import annotations

import difflib
import functools
import json
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import agno.db.mongo.mongo as agno_mongo_db
import pytest
from agno.agent import Agent
from agno.knowledge import Knowledge
from agno.team import Team
from agno.vectordb.mongodb import MongoDb as MongoVectorDb
from pymongo import MongoClient

from tests.golden.normalize import JsonValue, normalize

SNAPSHOT_DIR = Path(__file__).parent / "snapshots"


class GoldenSnapshot:
    """Compara ``data`` com o JSON salvo; com ``update=True`` regrava em vez de comparar."""

    def __init__(self, directory: Path, *, update: bool) -> None:
        self._dir = directory
        self._update = update

    @staticmethod
    def dumps(data: JsonValue) -> str:
        return json.dumps(data, indent=2, ensure_ascii=False, sort_keys=True) + "\n"

    def check(self, name: str, data: JsonValue) -> None:
        path = self._dir / f"{name}.json"
        current = self.dumps(data)
        if self._update:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(current, encoding="utf-8")
            return
        if not path.exists():
            pytest.fail(f"snapshot {path.name} não existe; gere com `pytest tests/golden --update-golden`")
        saved = path.read_text(encoding="utf-8")
        if saved != current:
            diff = "".join(
                difflib.unified_diff(
                    saved.splitlines(keepends=True),
                    current.splitlines(keepends=True),
                    fromfile=f"{path.name} (salvo)",
                    tofile=f"{path.name} (atual)",
                )
            )
            pytest.fail(
                f"kwargs de Agent/Team mudaram em {path.name}. Se a mudança é intencional, "
                f"rode `pytest tests/golden --update-golden` e revise o diff no commit.\n{diff}",
                pytrace=False,
            )


@pytest.fixture
def golden(request: pytest.FixtureRequest) -> GoldenSnapshot:
    return GoldenSnapshot(SNAPSHOT_DIR, update=bool(request.config.getoption("--update-golden")))


@dataclass
class AgnoCalls:
    """O que o espião viu, já normalizado, na ordem das chamadas."""

    constructors: list[dict[str, JsonValue]] = field(default_factory=list)
    knowledge_inserts: list[JsonValue] = field(default_factory=list)

    def of(self, cls_name: str) -> list[JsonValue]:
        return [c["kwargs"] for c in self.constructors if c["class"] == cls_name]


def _spy_init(cls: type, calls: AgnoCalls) -> Callable[..., None]:
    original = cls.__init__

    @functools.wraps(original)
    def spy(self: Any, *args: Any, **kwargs: Any) -> None:
        if args:
            raise AssertionError(f"{cls.__name__} recebeu argumentos posicionais: {args!r}")
        # normaliza ANTES do __init__ original, que muta o modelo (model_type etc.)
        calls.constructors.append({"class": cls.__name__, "kwargs": normalize(kwargs)})
        original(self, **kwargs)

    return spy


@pytest.fixture
def agno_calls(monkeypatch: pytest.MonkeyPatch) -> Iterator[AgnoCalls]:
    calls = AgnoCalls()
    clients: list[MongoClient[Any]] = []

    def lazy_client(*args: Any, **kwargs: Any) -> MongoClient[Any]:
        client: MongoClient[Any] = MongoClient(*args, connect=False, **kwargs)
        clients.append(client)
        return client

    def record_insert(self: Knowledge, *args: Any, **kwargs: Any) -> None:
        calls.knowledge_inserts.append(normalize({"args": list(args), "kwargs": kwargs}))

    def refuse_create(self: MongoVectorDb) -> None:
        raise AssertionError("golden: MongoVectorDb.create não deveria ser chamado")

    monkeypatch.setattr(Agent, "__init__", _spy_init(Agent, calls))
    monkeypatch.setattr(Team, "__init__", _spy_init(Team, calls))
    monkeypatch.setattr(agno_mongo_db, "MongoClient", lazy_client)
    monkeypatch.setattr(MongoVectorDb, "exists", lambda self: True)
    monkeypatch.setattr(MongoVectorDb, "create", refuse_create)
    monkeypatch.setattr(Knowledge, "insert", record_insert)
    yield calls
    for client in clients:
        client.close()
