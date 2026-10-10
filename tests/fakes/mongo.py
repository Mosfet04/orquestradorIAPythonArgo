"""Coleção e cliente Mongo (motor) em memória para os repositórios de ``src/infrastructure/repositories``.

Só o que os repositórios usam: ``find``/``find_one`` com filtro de igualdade em campos de topo
(ex.: ``{"active": True}``) e ``$in``, ``sort`` por um campo ou por uma lista de campos (no cursor ou
no kwarg ``sort=``), iteração com ``async for``, ``insert_many`` (com ``_id`` gerado e o índice único
de ``create_index(..., unique=True)`` emulado: repetido vira ``BulkWriteError`` código 11000, como no
pymongo) e ``count_documents``. Cada leitura devolve uma cópia do documento, como um documento novo
vindo do banco. Igualdade como no BSON: booleano só casa com booleano (``1`` não casa com ``True``).
"""

from __future__ import annotations

import copy
from collections import defaultdict
from typing import Any

from bson import ObjectId
from pymongo.errors import BulkWriteError

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


def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, expected in query.items():
        actual = doc.get(key)
        if isinstance(expected, dict):
            if set(expected) != {"$in"}:
                raise NotImplementedError(f"FakeMongoCollection: operador não suportado em {key!r}")
            if not any(_bson_equal(actual, option) for option in expected["$in"]):
                return False
        elif not _bson_equal(actual, expected):
            return False
    return True


class FakeMongoCollection:
    def __init__(self, docs: list[dict[str, Any]] | None = None) -> None:
        self.docs = list(docs or [])
        self.queries: list[dict[str, Any]] = []
        self.unique_keys: set[str] = set()
        self.indexes: list[dict[str, Any]] = []

    def find(self, query: dict[str, Any], *, sort: SortSpec | None = None) -> FakeAsyncCursor:
        self.queries.append(query)
        cursor = FakeAsyncCursor([d for d in self.docs if _matches(d, query)])
        return cursor.sort(sort) if sort is not None else cursor

    async def find_one(self, query: dict[str, Any], *, sort: SortSpec | None = None) -> dict[str, Any] | None:
        self.queries.append(query)
        found = [d for d in self.docs if _matches(d, query)]
        if sort is not None:
            found = _sorted(found, _sort_keys(sort, 1))
        return copy.deepcopy(found[0]) if found else None

    async def count_documents(self, query: dict[str, Any], *, limit: int = 0) -> int:
        self.queries.append(query)
        count = sum(1 for d in self.docs if _matches(d, query))
        return min(count, limit) if limit else count

    async def create_index(self, keys: SortSpec, *, name: str | None = None, unique: bool = False) -> str:
        fields = _sort_keys(keys, 1)
        self.indexes.append({"keys": fields, "name": name, "unique": unique})
        if unique and len(fields) == 1:
            self.unique_keys.add(fields[0][0])
        return name or "_".join(f"{k}_{d}" for k, d in fields)

    async def insert_many(self, docs: list[dict[str, Any]], *, ordered: bool = True) -> None:
        """Como o pymongo: ``_id`` gerado no documento passado; com ``ordered=False`` grava todos os que
        não violam índice único e depois levanta ``BulkWriteError`` com os repetidos."""
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
