"""Coleção e cliente Mongo (motor) em memória para os repositórios de ``src/infrastructure/repositories``
e para o db de sessões do agno (``SessionDb``, F2-08).

Só o que os repositórios e o ``AsyncMongoDb`` do agno 2.5.8 usam em sessões e memória:
``find``/``find_one`` com filtro de igualdade em campos de topo (ex.: ``{"active": True}``), ``$in``,
``$ne``, ``$exists`` e ``$or``, projeção de inclusão, ``sort`` por um campo ou por uma lista de campos (no
cursor ou no kwarg ``sort=``), ``skip``/``limit``/``to_list``, iteração com ``async for``,
``insert_many`` (com ``_id`` gerado), ``replace_one``/``find_one_and_replace`` (com ``upsert``),
``delete_one``/``delete_many``, ``distinct``, ``count_documents`` e o ``aggregate`` mínimo das
estatísticas de memória (``$match``, ``$group`` com ``$sum: 1``/``$max``, ``$sort``, ``$skip``,
``$limit``, ``$count``). O índice único de
``create_index(..., unique=True)`` é emulado: repetido vira ``BulkWriteError`` código 11000 no
``insert_many`` e ``DuplicateKeyError`` nas demais escritas, como no pymongo. Cada leitura devolve
uma cópia do documento, como um documento novo vindo do banco. Igualdade como no BSON: booleano só
casa com booleano (``1`` não casa com ``True``); campo ausente casa com ``None``; num campo de lista,
basta um item casar. O que não está aqui falha alto (``NotImplementedError``/``AttributeError``).

A fidelidade ao Mongo real é conferida pelos testes ``live`` (``mongo-live``, ``MONGO_LIVE_URL``).
"""

from __future__ import annotations

import copy
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any

import pytest
from bson import ObjectId
from pymongo import ReturnDocument
from pymongo.errors import BulkWriteError, DuplicateKeyError, InvalidOperation, ServerSelectionTimeoutError
from pymongo.synchronous.mongo_client import MongoClient as SyncMongoClient

from tests.fakes.models import running_on_event_loop

SortSpec = str | list[tuple[str, int]]

DUPLICATE_KEY = 11000


def _sort_keys(key_or_list: SortSpec, direction: int) -> list[tuple[str, int]]:
    return [(key_or_list, direction)] if isinstance(key_or_list, str) else list(key_or_list)


def _sorted(docs: list[dict[str, Any]], keys: list[tuple[str, int]]) -> list[dict[str, Any]]:
    """Ordenação estável campo a campo (do último para o primeiro); campo ausente vem antes,
    como no Mongo (``null`` é o menor valor)."""
    result = list(docs)
    for key, direction in reversed(keys):
        result.sort(key=lambda d, k=key: (k in d, d.get(k)), reverse=direction == -1)
    return result


class FakeAsyncCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._items = list(docs)
        self._docs = iter(self._items)
        self.sorts: list[tuple[str, int]] = []

    def sort(self, key_or_list: SortSpec, direction: int = 1) -> FakeAsyncCursor:
        """``cursor.sort(key, 1|-1)`` ou ``cursor.sort([(key, 1), ...])`` do motor (antes de iterar)."""
        keys = _sort_keys(key_or_list, direction)
        self.sorts.extend(keys)
        self._items = _sorted(self._items, keys)
        self._docs = iter(self._items)
        return self

    def skip(self, count: int) -> FakeAsyncCursor:
        self._items = self._items[count:]
        self._docs = iter(self._items)
        return self

    def limit(self, count: int) -> FakeAsyncCursor:
        """``limit(0)`` é sem limite, como no Mongo."""
        if count:
            self._items = self._items[:count]
            self._docs = iter(self._items)
        return self

    async def to_list(self, length: int | None = None) -> list[dict[str, Any]]:
        """Documentos restantes (cópias), até ``length``."""
        rest = [copy.deepcopy(doc) for doc in self._docs]
        return rest if length is None else rest[:length]

    def __aiter__(self) -> FakeAsyncCursor:
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return copy.deepcopy(next(self._docs))
        except StopIteration:
            raise StopAsyncIteration from None


def _bson_equal(actual: Any, expected: Any) -> bool:
    if isinstance(actual, bool) or isinstance(expected, bool):
        return type(actual) is type(expected) and actual == expected
    return bool(actual == expected)


def _field_equal(actual: Any, expected: Any) -> bool:
    """Igualdade de campo como no Mongo: num campo de lista, basta um item casar."""
    if _bson_equal(actual, expected):
        return True
    return isinstance(actual, list) and any(_bson_equal(item, expected) for item in actual)


def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, expected in query.items():
        if key == "$or":
            if not any(_matches(doc, option) for option in expected):
                return False
            continue
        if key.startswith("$"):
            raise NotImplementedError(f"FakeMongoCollection: operador não suportado: {key!r}")
        actual = doc.get(key)
        if isinstance(expected, dict) and any(k.startswith("$") for k in expected):
            if set(expected) == {"$in"}:
                if not any(_field_equal(actual, option) for option in expected["$in"]):
                    return False
            elif set(expected) == {"$exists"}:
                if (key in doc) is not bool(expected["$exists"]):
                    return False
            elif set(expected) == {"$ne"}:
                if _field_equal(actual, expected["$ne"]):
                    return False
            else:
                raise NotImplementedError(f"FakeMongoCollection: operador não suportado em {key!r}")
        elif not _field_equal(actual, expected):
            return False
    return True


def _projected(doc: dict[str, Any], projection: dict[str, Any] | None) -> dict[str, Any]:
    """Cópia do documento; com projeção de inclusão (``{"campo": 1}``), só esses campos e o ``_id``."""
    if not projection:
        return copy.deepcopy(doc)
    if not all(projection.values()):
        raise NotImplementedError("FakeMongoCollection: só projeção de inclusão")
    return {k: copy.deepcopy(v) for k, v in doc.items() if k == "_id" or k in projection}


def _aggregate(docs: list[dict[str, Any]], pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pipeline mínimo: ``$match``, ``$group`` (``_id: "$campo"``, ``$sum: 1``, ``$max: "$campo"``),
    ``$sort``, ``$skip``, ``$limit`` e ``$count``. Estágio ou acumulador fora disso falha alto."""
    rows = [copy.deepcopy(doc) for doc in docs]
    for stage in pipeline:
        ((name, spec),) = stage.items()
        if name == "$match":
            rows = [row for row in rows if _matches(row, spec)]
        elif name == "$group":
            groups: dict[Any, dict[str, Any]] = {}
            for row in rows:
                key = row.get(spec["_id"].removeprefix("$"))
                group = groups.setdefault(key, {"_id": key})
                for field_name, accumulator in spec.items():
                    if field_name == "_id":
                        continue
                    ((operator, operand),) = accumulator.items()
                    if operator == "$sum" and operand == 1:
                        group[field_name] = group.get(field_name, 0) + 1
                    elif operator == "$max" and isinstance(operand, str):
                        value = row.get(operand.removeprefix("$"))
                        current = group.get(field_name)
                        if current is None or (value is not None and value > current):
                            group[field_name] = value
                    else:
                        raise NotImplementedError(f"FakeMongoCollection.aggregate: acumulador fora do fake: {operator}")
            rows = list(groups.values())
        elif name == "$sort":
            rows = _sorted(rows, list(spec.items()))
        elif name == "$skip":
            rows = rows[spec:]
        elif name == "$limit":
            rows = rows[:spec]
        elif name == "$count":
            rows = [{spec: len(rows)}] if rows else []
        else:
            raise NotImplementedError(f"FakeMongoCollection.aggregate: estágio não suportado: {name}")
    return rows


class FakeWriteResult:
    """``UpdateResult``/``DeleteResult`` do pymongo: só os contadores e o ``upserted_id``."""

    def __init__(self, *, matched: int = 0, deleted: int = 0, upserted_id: Any = None) -> None:
        self.matched_count = matched
        self.modified_count = matched
        self.deleted_count = deleted
        self.upserted_id = upserted_id


class FakeMongoCollection:
    def __init__(self, docs: list[dict[str, Any]] | None = None) -> None:
        self.docs = list(docs or [])
        self.queries: list[dict[str, Any]] = []
        self.unique_keys: set[str] = set()
        self.indexes: list[dict[str, Any]] = []
        self.client: FakeMotorClient | None = None
        """Cliente dono da coleção (``FakeMotorClient``): depois do ``close`` dele, toda operação falha."""

    def _use(self, query: dict[str, Any] | None = None) -> None:
        if self.client is not None:
            self.client.refuse_if_closed()
        if query is not None:
            self.queries.append(query)

    def _refuse_duplicate(self, doc: dict[str, Any], *, replacing: dict[str, Any] | None = None) -> None:
        for key in self.unique_keys:
            if key in doc and any(d is not replacing and _bson_equal(d.get(key), doc[key]) for d in self.docs):
                raise DuplicateKeyError("E11000 duplicate key error", DUPLICATE_KEY)

    def find(
        self, query: dict[str, Any], projection: dict[str, Any] | None = None, *, sort: SortSpec | None = None
    ) -> FakeAsyncCursor:
        self._use(query)
        cursor = FakeAsyncCursor([_projected(d, projection) for d in self.docs if _matches(d, query)])
        return cursor.sort(sort) if sort is not None else cursor

    async def find_one(
        self, query: dict[str, Any], projection: dict[str, Any] | None = None, *, sort: SortSpec | None = None
    ) -> dict[str, Any] | None:
        self._use(query)
        found = [d for d in self.docs if _matches(d, query)]
        if sort is not None:
            found = _sorted(found, _sort_keys(sort, 1))
        return _projected(found[0], projection) if found else None

    async def count_documents(self, query: dict[str, Any], *, limit: int = 0) -> int:
        self._use(query)
        count = sum(1 for d in self.docs if _matches(d, query))
        return min(count, limit) if limit else count

    async def distinct(self, key: str, query: dict[str, Any] | None = None) -> list[Any]:
        """Valores distintos de ``key``; campo de lista conta item a item, como no Mongo."""
        self._use(query or {})
        values: list[Any] = []
        for doc in self.docs:
            if key not in doc or not _matches(doc, query or {}):
                continue
            for value in doc[key] if isinstance(doc[key], list) else [doc[key]]:
                if value not in values:
                    values.append(value)
        return values

    def aggregate(self, pipeline: list[dict[str, Any]]) -> FakeAsyncCursor:
        """Como no motor: devolve o cursor na hora (``await collection.aggregate(p).to_list(...)``)."""
        self._use()
        return FakeAsyncCursor(_aggregate(self.docs, pipeline))

    async def replace_one(
        self, query: dict[str, Any], replacement: dict[str, Any], upsert: bool = False
    ) -> FakeWriteResult:
        """Troca o primeiro documento casado (o ``_id`` fica); sem casar e com ``upsert``, insere."""
        self._use(query)
        found = next((d for d in self.docs if _matches(d, query)), None)
        if found is not None:
            self._refuse_duplicate(replacement, replacing=found)
            kept_id = found["_id"]
            found.clear()
            found.update(copy.deepcopy(replacement), _id=kept_id)
            return FakeWriteResult(matched=1)
        if not upsert:
            return FakeWriteResult()
        self._refuse_duplicate(replacement)
        inserted = {"_id": ObjectId(), **copy.deepcopy(replacement)}
        self.docs.append(inserted)
        return FakeWriteResult(upserted_id=inserted["_id"])

    async def find_one_and_replace(
        self,
        filter: dict[str, Any],  # nome do parâmetro no pymongo (o agno passa por nome)
        replacement: dict[str, Any],
        *,
        upsert: bool = False,
        return_document: bool = ReturnDocument.BEFORE,
    ) -> dict[str, Any] | None:
        before = await self.find_one(filter)
        await self.replace_one(filter, replacement, upsert=upsert)
        if return_document == ReturnDocument.BEFORE:
            return before
        after = next((d for d in self.docs if _matches(d, filter)), None)
        return copy.deepcopy(after) if after is not None else None

    async def delete_one(self, query: dict[str, Any]) -> FakeWriteResult:
        self._use(query)
        found = next((d for d in self.docs if _matches(d, query)), None)
        if found is None:
            return FakeWriteResult()
        self.docs.remove(found)
        return FakeWriteResult(deleted=1)

    async def delete_many(self, query: dict[str, Any]) -> FakeWriteResult:
        self._use(query)
        kept = [d for d in self.docs if not _matches(d, query)]
        deleted = len(self.docs) - len(kept)
        self.docs[:] = kept
        return FakeWriteResult(deleted=deleted)

    async def create_index(self, keys: SortSpec, *, name: str | None = None, unique: bool = False) -> str:
        self._use()
        fields = _sort_keys(keys, 1)
        self.indexes.append({"keys": fields, "name": name, "unique": unique})
        if unique and len(fields) == 1:
            self.unique_keys.add(fields[0][0])
        return name or "_".join(f"{k}_{d}" for k, d in fields)

    async def insert_many(self, docs: list[dict[str, Any]], *, ordered: bool = True) -> None:
        """Como o pymongo: ``_id`` gerado no documento passado; com ``ordered=False`` grava todos os que
        não violam índice único e depois levanta ``BulkWriteError`` com os repetidos."""
        self._use()
        errors: list[dict[str, Any]] = []
        inserted = 0
        for index, doc in enumerate(docs):
            doc.setdefault("_id", ObjectId())
            if any(any(_bson_equal(d.get(k), doc.get(k)) for d in self.docs) for k in self.unique_keys if k in doc):
                errors.append({"index": index, "code": DUPLICATE_KEY, "errmsg": "E11000 duplicate key error"})
                if ordered:
                    break
                continue
            self.docs.append(copy.deepcopy(doc))
            inserted += 1
        if errors:
            raise BulkWriteError({"writeErrors": errors, "nInserted": inserted, "writeConcernErrors": []})


class FakeMongoClient:
    """``client[db][collection]``: qualquer database expõe as mesmas coleções."""

    def __init__(self, collections: dict[str, FakeMongoCollection]) -> None:
        self.collections = collections

    def __getitem__(self, database_name: str) -> dict[str, FakeMongoCollection]:
        return self.collections


class FakeMotorDatabase:
    """``database[collection]``: a coleção nasce no primeiro acesso, como no Mongo."""

    def __init__(self, client: FakeMotorClient) -> None:
        self._client = client
        self.collections: dict[str, FakeMongoCollection] = {}

    def __getitem__(self, collection_name: str) -> FakeMongoCollection:
        if collection_name not in self.collections:
            collection = FakeMongoCollection()
            collection.client = self._client
            self.collections[collection_name] = collection
        return self.collections[collection_name]

    async def list_collection_names(self) -> list[str]:
        """Só as coleções que existiriam no servidor: com documento ou índice."""
        self._client.refuse_if_closed()
        return [name for name, collection in self.collections.items() if collection.docs or collection.indexes]


class FakeMotorClient:
    """Cliente motor em memória com um database por nome (``client[db][collection]``).

    O nome da classe tem ``Motor`` de propósito: o ``AsyncMongoDb`` do agno 2.5.8 reconhece o tipo
    do cliente injetado pelo nome da classe (``_detect_client_type``). ``close`` é síncrono, como
    no motor, e registra em ``closes`` se rodou na thread do event loop; depois dele toda operação
    levanta ``InvalidOperation``, como o pymongo.
    """

    def __init__(self) -> None:
        self.databases: dict[str, FakeMotorDatabase] = {}
        self.closes: list[bool] = []
        """Por ``close``: ``True`` se rodou na thread do event loop (I/O síncrono no loop)."""

    def __getitem__(self, database_name: str) -> FakeMotorDatabase:
        if database_name not in self.databases:
            self.databases[database_name] = FakeMotorDatabase(self)
        return self.databases[database_name]

    def refuse_if_closed(self) -> None:
        if self.closes:
            raise InvalidOperation("Cannot use MongoClient after close")

    def close(self) -> None:
        self.closes.append(running_on_event_loop())


class FailingMongoCollection:
    """Coleção de um servidor fora do ar: toda operação levanta ``error`` (o erro do driver)."""

    def __init__(self, error: Exception) -> None:
        self._error = error

    def find(self, *_: object, **__: object) -> FakeAsyncCursor:
        raise self._error

    async def find_one(self, *_: object, **__: object) -> dict[str, Any] | None:
        raise self._error

    async def count_documents(self, *_: object, **__: object) -> int:
        raise self._error

    async def create_index(self, *_: object, **__: object) -> str:
        raise self._error

    async def insert_many(self, *_: object, **__: object) -> None:
        raise self._error


class FailingMongoClient:
    """Cliente de um servidor fora do ar: ``admin.command`` e toda coleção levantam ``error``."""

    def __init__(self, error: Exception) -> None:
        self._error = error
        self.admin = self

    def __getitem__(self, database_name: str) -> defaultdict[str, FailingMongoCollection]:
        return defaultdict(lambda: FailingMongoCollection(self._error))

    async def command(self, *_: object) -> dict[str, Any]:
        raise self._error

    def close(self) -> None:
        """Nada a fechar."""


@dataclass
class SyncPymongoCalls:
    """O que o ``trap_sync_pymongo`` viu: cada item diz se rodou na thread do event loop."""

    operations: list[tuple[str, bool]] = field(default_factory=list)
    """``(operação, no event loop)`` de cada operação do pymongo síncrono (``find``, ``createIndexes``...)."""
    closes: list[bool] = field(default_factory=list)
    """Um item por ``MongoClient.close``."""

    def on_event_loop(self) -> list[str]:
        """Operações que rodaram na thread do event loop (I/O síncrono no caminho async)."""
        return [operation for operation, on_loop in self.operations if on_loop]


def trap_sync_pymongo(monkeypatch: pytest.MonkeyPatch) -> SyncPymongoCalls:
    """Detector de pymongo síncrono no event loop (F2-08), sem rede.

    Toda operação do pymongo 4.18 síncrono escolhe um servidor antes de falar com ele
    (``MongoClient._select_server``), inclusive as que o motor delega às threads do executor. Aqui
    essa escolha registra a operação e a thread e falha na hora com ``ServerSelectionTimeoutError``
    (o erro de um Mongo fora do ar): nenhum socket é aberto. ``MongoClient.close`` também é
    registrado. Vale para clientes reais (``MongoClient``, ``AsyncIOMotorClient``) criados no teste.
    """
    calls = SyncPymongoCalls()
    original_close = SyncMongoClient.close

    def select_server(self: Any, server_selector: Any, session: Any, operation: Any, *_: Any, **__: Any) -> Any:
        calls.operations.append((str(getattr(operation, "value", operation)), running_on_event_loop()))
        raise ServerSelectionTimeoutError("trap_sync_pymongo: sem servidor (detector)")

    def close(self: Any) -> None:
        calls.closes.append(running_on_event_loop())
        original_close(self)

    monkeypatch.setattr(SyncMongoClient, "_select_server", select_server)
    monkeypatch.setattr(SyncMongoClient, "close", close)
    return calls
