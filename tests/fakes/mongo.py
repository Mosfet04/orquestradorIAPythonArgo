"""Coleção e cliente Mongo (motor) em memória para os repositórios de ``src/infrastructure/repositories``.

Só o que os repositórios de config usam: ``find`` com filtro de igualdade em campos de topo
(ex.: ``{"active": True}``), ``sort`` por um campo, iteração com ``async for`` e ``find_one``. Cada leitura devolve uma
cópia do documento, como um documento novo vindo do banco.
"""

from __future__ import annotations

import copy
from typing import Any


class FakeAsyncCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._items = list(docs)
        self._docs = iter(self._items)
        self.sorts: list[tuple[str, int]] = []

    def sort(self, key: str, direction: int = 1) -> FakeAsyncCursor:
        """``cursor.sort(key, 1|-1)`` do motor (chamado antes de iterar); campo ausente vem antes,
        como no Mongo (``null`` é o menor valor)."""
        self.sorts.append((key, direction))
        self._items.sort(key=lambda d: (key in d, d.get(key)), reverse=direction == -1)
        self._docs = iter(self._items)
        return self

    def __aiter__(self) -> FakeAsyncCursor:
        return self

    async def __anext__(self) -> dict[str, Any]:
        try:
            return copy.deepcopy(next(self._docs))
        except StopIteration:
            raise StopAsyncIteration from None


def _matches(doc: dict[str, Any], query: dict[str, Any]) -> bool:
    for key, expected in query.items():
        if isinstance(expected, dict):
            raise NotImplementedError(f"FakeMongoCollection: operador não suportado em {key!r}")
        if doc.get(key) != expected:
            return False
    return True


class FakeMongoCollection:
    def __init__(self, docs: list[dict[str, Any]] | None = None) -> None:
        self.docs = list(docs or [])
        self.queries: list[dict[str, Any]] = []

    def find(self, query: dict[str, Any]) -> FakeAsyncCursor:
        self.queries.append(query)
        return FakeAsyncCursor([d for d in self.docs if _matches(d, query)])

    async def find_one(self, query: dict[str, Any]) -> dict[str, Any] | None:
        self.queries.append(query)
        found = next((d for d in self.docs if _matches(d, query)), None)
        return copy.deepcopy(found) if found is not None else None


class FakeMongoClient:
    """``client[db][collection]``: qualquer database expõe as mesmas coleções."""

    def __init__(self, collections: dict[str, FakeMongoCollection]) -> None:
        self.collections = collections

    def __getitem__(self, database_name: str) -> dict[str, FakeMongoCollection]:
        return self.collections
