"""Mongo síncrono (pymongo) em memória para o vector db ``MongoDb`` do agno 2.5.8 (F2-11).

Substitui o ``MongoClient`` que ``agno/vectordb/mongodb/mongodb.py`` importa (corte na borda do
driver, como o ``cut_agno_io`` faz com o db de sessões): o ``MongoDb`` do agno e a subclasse do
runtime rodam o código real (``create``, ``upsert``, ``prepare_doc``, índice de busca) contra este
servidor. Só o que eles usam: ``admin.command("ping")``, ``list_collection_names``,
``create_collection``, ``find``/``find_one`` (igualdade, ``$in``, ``$exists`` e ``$or``, com caminho
pontilhado e índice de lista, ex.: ``embedding.767``; projeção de inclusão), ``count_documents``,
``update_one`` com ``$set`` (e ``upsert``), ``insert_many``, ``delete_many`` e os índices de busca
(``list_search_indexes``, ``create_search_index``, ``drop_search_index``; o índice criado já nasce
``READY``/``queryable``, com a definição em ``latestDefinition`` como no Atlas). O resto falha alto.

O estado vive no ``FakeVectorServer``: clientes novos (um por vector db, como no agno) enxergam os
mesmos dados, então dois ``create_agent`` seguidos simulam dois startups do processo.
"""

from __future__ import annotations

import copy
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import agno.vectordb.mongodb.mongodb as agno_vector_module
import pytest
from pymongo.operations import SearchIndexModel

from tests.fakes.models import running_on_event_loop
from tests.fakes.mongo import _field_equal, _projected

_MISSING = object()


def _value_at(doc: Any, path: str) -> Any:
    """Valor do caminho pontilhado (``a.b``, ``lista.3``); ``_MISSING`` se não existir."""
    current = doc
    for part in path.split("."):
        if isinstance(current, dict):
            current = current.get(part, _MISSING)
        elif isinstance(current, list) and part.isdigit() and int(part) < len(current):
            current = current[int(part)]
        else:
            return _MISSING
        if current is _MISSING:
            return _MISSING
    return current


def _equal(actual: Any, expected: Any) -> bool:
    """Igualdade de campo de ``tests/fakes/mongo.py`` (BSON: int e float casam; bool só com bool)."""
    if actual is _MISSING:
        return expected is None
    return _field_equal(actual, expected)


def _condition(actual: Any, expected: Any) -> bool:
    if not (isinstance(expected, dict) and any(k.startswith("$") for k in expected)):
        return _equal(actual, expected)
    if set(expected) == {"$exists"}:
        return (actual is not _MISSING) is bool(expected["$exists"])
    if set(expected) == {"$in"}:
        return any(_equal(actual, option) for option in expected["$in"])
    raise NotImplementedError(f"FakeVectorCollection: operador não suportado: {sorted(expected)!r}")


def matches(doc: dict[str, Any], query: dict[str, Any] | None) -> bool:
    """Filtro do Mongo no subconjunto descrito no módulo."""
    for key, expected in (query or {}).items():
        if key == "$or":
            if not any(matches(doc, option) for option in expected):
                return False
        elif key.startswith("$"):
            raise NotImplementedError(f"FakeVectorCollection: operador não suportado: {key!r}")
        elif not _condition(_value_at(doc, key), expected):
            return False
    return True


@dataclass
class _Result:
    matched_count: int = 0
    deleted_count: int = 0
    upserted_id: Any = None


@dataclass
class FakeVectorCollection:
    """Coleção com documentos por ``_id`` e índices de busca (``vectorSearch``)."""

    name: str
    docs: dict[Any, dict[str, Any]] = field(default_factory=dict)
    search_indexes: list[dict[str, Any]] = field(default_factory=list)
    dropped_search_indexes: list[str] = field(default_factory=list)
    created_search_indexes: list[dict[str, Any]] = field(default_factory=list)
    ops: list[tuple[str, bool]] = field(default_factory=list)
    """``(operação, rodou na thread do event loop)`` de cada chamada."""
    server: FakeVectorServer | None = field(default=None, repr=False)
    database: str = ""

    def _use(self, op: str) -> None:
        self.ops.append((op, running_on_event_loop()))

    def _materialize(self) -> None:
        if self.server is not None:
            self.server.created.add((self.database, self.name))

    # ── leitura ──
    def find_one(self, query: dict[str, Any] | None = None, projection: dict[str, Any] | None = None) -> Any:
        self._use("find_one")
        found = next((d for d in self.docs.values() if matches(d, query)), None)
        return _projected(found, projection) if found is not None else None

    def find(self, query: dict[str, Any] | None = None, projection: dict[str, Any] | None = None) -> Iterator[Any]:
        self._use("find")
        return iter([_projected(d, projection) for d in self.docs.values() if matches(d, query)])

    def count_documents(self, query: dict[str, Any], limit: int = 0) -> int:
        self._use("count_documents")
        count = sum(1 for d in self.docs.values() if matches(d, query))
        return min(count, limit) if limit else count

    # ── escrita ──
    def update_one(self, query: dict[str, Any], update: dict[str, Any], upsert: bool = False) -> _Result:
        self._use("update_one")
        if set(update) != {"$set"}:
            raise NotImplementedError("FakeVectorCollection: update_one só com $set")
        found = next((d for d in self.docs.values() if matches(d, query)), None)
        if found is not None:
            found.update(copy.deepcopy(update["$set"]))
            return _Result(matched_count=1)
        if not upsert:
            return _Result()
        doc = {**{k: v for k, v in query.items() if not k.startswith("$")}, **copy.deepcopy(update["$set"])}
        self.docs[doc["_id"]] = doc
        self._materialize()
        return _Result(upserted_id=doc["_id"])

    def insert_many(self, docs: list[dict[str, Any]], ordered: bool = True) -> None:
        self._use("insert_many")
        for doc in docs:
            self.docs[doc["_id"]] = copy.deepcopy(doc)
        self._materialize()

    def delete_many(self, query: dict[str, Any]) -> _Result:
        self._use("delete_many")
        doomed = [key for key, d in self.docs.items() if matches(d, query)]
        for key in doomed:
            del self.docs[key]
        return _Result(deleted_count=len(doomed))

    # ── índices de busca ──
    def list_search_indexes(self, name: str | None = None, **_: Any) -> Iterator[dict[str, Any]]:
        self._use("list_search_indexes")
        return iter([copy.deepcopy(i) for i in self.search_indexes if name is None or i["name"] == name])

    def create_search_index(self, model: SearchIndexModel) -> str:
        self._use("create_search_index")
        document = dict(model.document)
        if any(i["name"] == document["name"] for i in self.search_indexes):
            raise AssertionError(f"FakeVectorCollection: índice {document['name']!r} já existe")
        index = {
            "name": document["name"],
            "type": document.get("type", "search"),
            "status": "READY",
            "queryable": True,
            "latestDefinition": copy.deepcopy(document["definition"]),
        }
        self.search_indexes.append(index)
        self.created_search_indexes.append(copy.deepcopy(index))
        self._materialize()
        return str(document["name"])

    def drop_search_index(self, name: str) -> None:
        self._use("drop_search_index")
        self.search_indexes = [i for i in self.search_indexes if i["name"] != name]
        self.dropped_search_indexes.append(name)

    def index_information(self) -> dict[str, Any]:
        return {}


def vector_index(num_dimensions: int, name: str = "vector_index_1") -> dict[str, Any]:
    """Índice ``vectorSearch`` como o agno cria (``path=embedding``, cosseno), já pronto."""
    return {
        "name": name,
        "type": "vectorSearch",
        "status": "READY",
        "queryable": True,
        "latestDefinition": {
            "fields": [
                {"type": "vector", "numDimensions": num_dimensions, "path": "embedding", "similarity": "cosine"}
            ]
        },
    }


class FakeVectorDatabase:
    def __init__(self, server: FakeVectorServer, name: str) -> None:
        self._server = server
        self._name = name

    def __getitem__(self, collection: str) -> FakeVectorCollection:
        return self._server.collection(self._name, collection)

    def list_collection_names(self) -> list[str]:
        return sorted(c for db, c in self._server.created if db == self._name)

    def create_collection(self, name: str) -> FakeVectorCollection:
        self._server.created.add((self._name, name))
        return self[name]


class _Admin:
    def command(self, name: str) -> dict[str, Any]:
        if name != "ping":
            raise NotImplementedError(f"FakeVectorMongoClient: comando {name!r}")
        return {"ok": 1}


class FakeVectorMongoClient:
    def __init__(self, server: FakeVectorServer) -> None:
        self._server = server
        self.admin = _Admin()

    def __getitem__(self, database: str) -> FakeVectorDatabase:
        return FakeVectorDatabase(self._server, database)

    def get_database(self, database: str) -> FakeVectorDatabase:
        return self[database]

    def close(self) -> None:
        return None


@dataclass
class FakeVectorServer:
    """Estado do servidor: coleções por ``(banco, nome)`` e as que existem de fato no banco."""

    collections: dict[tuple[str, str], FakeVectorCollection] = field(default_factory=dict)
    created: set[tuple[str, str]] = field(default_factory=set)
    clients: int = 0

    def collection(self, database: str, name: str) -> FakeVectorCollection:
        key = (database, name)
        if key not in self.collections:
            self.collections[key] = FakeVectorCollection(name=name, server=self, database=database)
        return self.collections[key]

    def seed(
        self,
        database: str,
        name: str,
        docs: list[dict[str, Any]],
        *,
        index_dimensions: int | None = None,
    ) -> FakeVectorCollection:
        """Coleção já gravada (ex.: legado com ``embedding: []``), com índice opcional."""
        collection = self.collection(database, name)
        for doc in docs:
            collection.docs[doc["_id"]] = copy.deepcopy(doc)
        if index_dimensions is not None:
            collection.search_indexes.append(vector_index(index_dimensions))
        self.created.add((database, name))
        return collection

    def client(self, *_: Any, **__: Any) -> FakeVectorMongoClient:
        """No lugar de ``pymongo.MongoClient(url, **kwargs)``: ignora URL e opções."""
        self.clients += 1
        return FakeVectorMongoClient(self)


def install_fake_vector_mongo(
    monkeypatch: pytest.MonkeyPatch, server: FakeVectorServer | None = None
) -> FakeVectorServer:
    """Troca o ``MongoClient`` do vector db do agno pelo servidor em memória."""
    server = server or FakeVectorServer()
    monkeypatch.setattr(agno_vector_module, "MongoClient", server.client)
    return server


@pytest.fixture
def fake_vector_mongo(monkeypatch: pytest.MonkeyPatch) -> FakeVectorServer:
    """Servidor Mongo síncrono em memória para o vector db do RAG semântico."""
    return install_fake_vector_mongo(monkeypatch)
