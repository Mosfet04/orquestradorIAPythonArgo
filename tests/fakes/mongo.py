"""Coleção e clientes Mongo em memória: os do pymongo nativo (``FakeAsyncMongoClient``, F3-09; o
``FakeMongoClient`` de coleções fixas) para o db de sessões do agno (``SessionDb``, F2-08) e para os
repositórios de ``src/infrastructure/repositories`` (F3-09b), e o do motor (``FakeMotorClient``), que só
resta nos testes que comparam os dois drivers.

Só o que os repositórios e o ``AsyncMongoDb`` do agno 2.5.8 usam em sessões e memória:
``find``/``find_one`` com filtro de igualdade em campos de topo (ex.: ``{"active": True}``), ``$in``,
``$nin``, ``$ne``, ``$exists``, ``$gt``, ``$size``, ``$not`` e ``$or``,
projeção de inclusão, ``sort`` por um campo ou por uma lista de campos (no
cursor ou no kwarg ``sort=``), ``skip``/``limit``/``to_list``, iteração com ``async for``,
``insert_one``/``insert_many`` (com ``_id`` gerado), ``replace_one``/``find_one_and_replace`` (com ``upsert``),
``find_one_and_update`` só com ``$inc`` (com ``upsert``), ``update_one`` só com ``$set`` (com ``upsert``),
``index_information``, ``drop``,
``delete_one``/``delete_many``, ``distinct``, ``count_documents`` e o ``aggregate`` mínimo das
estatísticas de memória (``$match``, ``$group`` com ``$sum: 1``/``$max``, ``$sort``, ``$skip``,
``$limit``, ``$count``), dos runs da sessão do agno 3.x (``$addFields`` só com
``{"$ifNull": ["$campo", default]}``) e da volta da migração v3.0.0 (``$group`` com ``$push``). O índice único de
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
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest
from bson import ObjectId
from pymongo import AsyncMongoClient, ReturnDocument
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

    def batch_size(self, size: int) -> FakeAsyncCursor:
        """Só o encadeamento do motor: em memória não há lote."""
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
        if isinstance(expected, dict) and any(k.startswith("$") for k in expected):
            if not _operator_matches(doc, key, expected):
                return False
        elif not _field_equal(doc.get(key), expected):
            return False
    return True


def _operator_matches(doc: dict[str, Any], key: str, expected: dict[str, Any]) -> bool:
    """Condição de operador num campo: ``$in``, ``$nin``, ``$exists``, ``$ne``, ``$gt``, ``$size`` e
    ``$not`` (de outra condição de operador); vários operadores no mesmo campo valem juntos (E), como
    no Mongo. Outro operador falha alto.

    ``$nin`` é a negação do ``$in``: campo nulo ou ausente passa, salvo se ``None`` estiver na lista.
    ``$size`` só casa com lista do tamanho dado; ``$gt`` só compara valores do mesmo tipo numérico.
    """
    return all(_single_operator_matches(doc, key, operator, operand) for operator, operand in expected.items())


def _in(doc: dict[str, Any], key: str, operand: Any) -> bool:
    return any(_field_equal(doc.get(key), option) for option in operand)


def _gt(doc: dict[str, Any], key: str, operand: Any) -> bool:
    actual = doc.get(key)
    return isinstance(actual, (int, float)) and not isinstance(actual, bool) and actual > operand


def _not(doc: dict[str, Any], key: str, operand: Any) -> bool:
    if not (isinstance(operand, dict) and all(k.startswith("$") for k in operand)):
        raise NotImplementedError(f"FakeMongoCollection: $not só com operadores em {key!r}: {operand!r}")
    return not _operator_matches(doc, key, operand)


_OPERATORS: dict[str, Callable[[dict[str, Any], str, Any], bool]] = {
    "$in": _in,
    "$nin": lambda doc, key, operand: not _in(doc, key, operand),
    "$exists": lambda doc, key, operand: (key in doc) is bool(operand),
    "$ne": lambda doc, key, operand: not _field_equal(doc.get(key), operand),
    "$gt": _gt,
    "$size": lambda doc, key, operand: isinstance(doc.get(key), list) and len(doc[key]) == operand,
    "$not": _not,
}


def _single_operator_matches(doc: dict[str, Any], key: str, operator: str, operand: Any) -> bool:
    check = _OPERATORS.get(operator)
    if check is None:
        raise NotImplementedError(f"FakeMongoCollection: operador não suportado em {key!r}: {operator}")
    return check(doc, key, operand)


def _projected(doc: dict[str, Any], projection: dict[str, Any] | None) -> dict[str, Any]:
    """Cópia do documento; com projeção de inclusão (``{"campo": 1}``), só esses campos e o ``_id``.

    Caminho com um ponto (``{"runs.run_id": 1}``) projeta o subcampo do subdocumento ou de cada
    subdocumento do array, como o Mongo (item que não é documento sai; documento sem o subcampo vira ``{}``).
    """
    if not projection:
        return copy.deepcopy(doc)
    if not all(projection.values()):
        raise NotImplementedError("FakeMongoCollection: só projeção de inclusão")
    projected = {k: copy.deepcopy(v) for k, v in doc.items() if k == "_id" or k in projection}
    dotted = [path.split(".") for path in projection if "." in path]
    if len({parts[0] for parts in dotted}) != len(dotted) or any(len(parts) != 2 for parts in dotted):
        raise NotImplementedError(f"FakeMongoCollection: projeção pontilhada não suportada: {projection}")
    for head, subfield in dotted:
        value = doc.get(head)
        if isinstance(value, dict):
            projected[head] = _pick(value, subfield)
        elif isinstance(value, list):
            projected[head] = [_pick(item, subfield) for item in value if isinstance(item, dict)]
    return projected


def _pick(subdoc: dict[str, Any], field: str) -> dict[str, Any]:
    return {field: copy.deepcopy(subdoc[field])} if field in subdoc else {}


def _aggregate(docs: list[dict[str, Any]], pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Pipeline mínimo: ``$match``, ``$addFields`` (só ``$ifNull``), ``$group`` (``_id: "$campo"``,
    ``$sum: 1``, ``$max: "$campo"``, ``$push: "$campo"``), ``$sort``, ``$skip``, ``$limit`` e ``$count``. Estágio,
    expressão ou acumulador fora disso falha alto."""
    rows = [copy.deepcopy(doc) for doc in docs]
    for stage in pipeline:
        ((name, spec),) = stage.items()
        if name == "$match":
            rows = [row for row in rows if _matches(row, spec)]
        elif name == "$addFields":
            for row in rows:
                row.update({field_name: _if_null(row, expression) for field_name, expression in spec.items()})
        elif name == "$group":
            rows = _group(rows, spec)
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


def _is_expression(value: Any) -> bool:
    """``True`` se o valor seria avaliado como expressão pelo Mongo (``"$campo"``, ``{"$op": ...}``)."""
    if isinstance(value, str):
        return value.startswith("$")
    if isinstance(value, dict):
        return any(key.startswith("$") or _is_expression(item) for key, item in value.items())
    if isinstance(value, list):
        return any(_is_expression(item) for item in value)
    return False


def _if_null(row: dict[str, Any], expression: Any) -> Any:
    """Expressão ``{"$ifNull": ["$campo", default]}``: o valor do campo, ou o default se nulo ou ausente.

    Só a forma que o agno 3.x usa nos runs da sessão (campo de topo, default literal); o Mongo real
    avaliaria diferente caminho pontilhado, default que é expressão e campo sem ``$``, e o fake não
    os emula: qualquer outra forma falha alto.
    """
    if not (isinstance(expression, dict) and set(expression) == {"$ifNull"}):
        raise NotImplementedError(f"FakeMongoCollection.aggregate: expressão fora do fake: {expression!r}")
    args = expression["$ifNull"]
    if not (isinstance(args, (list, tuple)) and len(args) == 2):
        raise NotImplementedError(f"FakeMongoCollection.aggregate: $ifNull só com [campo, default]: {args!r}")
    field_path, default = args
    if not (isinstance(field_path, str) and field_path.startswith("$") and not field_path.startswith("$$")):
        raise NotImplementedError(f"FakeMongoCollection.aggregate: $ifNull exige '$campo': {field_path!r}")
    field_name = field_path.removeprefix("$")
    if not field_name or "." in field_name:
        raise NotImplementedError(f"FakeMongoCollection.aggregate: $ifNull só com campo de topo: {field_path!r}")
    if _is_expression(default):
        raise NotImplementedError(f"FakeMongoCollection.aggregate: $ifNull com default expressão: {default!r}")
    value = row.get(field_name)
    return default if value is None else value


def _group(rows: list[dict[str, Any]], spec: dict[str, Any]) -> list[dict[str, Any]]:
    """Estágio ``$group``: um grupo por valor de ``_id: "$campo"``, na ordem de aparição."""
    groups: dict[Any, dict[str, Any]] = {}
    for row in rows:
        key = row.get(spec["_id"].removeprefix("$"))
        group = groups.setdefault(key, {"_id": key})
        for field_name, accumulator in spec.items():
            if field_name != "_id":
                _accumulate(group, field_name, accumulator, row)
    return list(groups.values())


def _accumulate(group: dict[str, Any], field_name: str, accumulator: dict[str, Any], row: dict[str, Any]) -> None:
    """Aplica ``$sum: 1``, ``$max: "$campo"`` ou ``$push: "$campo"`` de uma linha ao grupo; outro
    acumulador falha alto."""
    ((operator, operand),) = accumulator.items()
    if operator == "$push" and isinstance(operand, str) and operand.startswith("$"):
        group.setdefault(field_name, []).append(row.get(operand.removeprefix("$")))
    elif operator == "$sum" and operand == 1:
        group[field_name] = group.get(field_name, 0) + 1
    elif operator == "$max" and isinstance(operand, str):
        value = row.get(operand.removeprefix("$"))
        current = group.get(field_name)
        if current is None or (value is not None and value > current):
            group[field_name] = value
    else:
        raise NotImplementedError(f"FakeMongoCollection.aggregate: acumulador fora do fake: {operator}")


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
        self.client: _FakeClient | FakeMongoClient | None = None
        """Cliente dono da coleção: depois do ``close`` dele, toda operação falha."""

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

    async def update_one(
        self, filter: dict[str, Any], update: dict[str, Any], upsert: bool = False  # pylint: disable=redefined-builtin
    ) -> FakeWriteResult:
        """Só ``$set`` (carimbo de versão do esquema, volta da migração); sem casar e com ``upsert``,
        cria pelos campos de igualdade do filtro."""
        if set(update) != {"$set"}:
            raise NotImplementedError(f"update não suportado pelo fake: {sorted(update)}")
        self._use(filter)
        found = next((d for d in self.docs if _matches(d, filter)), None)
        if found is None and not upsert:
            return FakeWriteResult()
        changed = {**(found or {k: v for k, v in filter.items() if not isinstance(v, dict)}), **update["$set"]}
        self._refuse_duplicate(changed, replacing=found)
        if found is None:
            inserted = {"_id": ObjectId(), **copy.deepcopy(changed)}
            self.docs.append(inserted)
            return FakeWriteResult(upserted_id=inserted["_id"])
        found.update(copy.deepcopy(update["$set"]))
        return FakeWriteResult(matched=1)

    async def find_one_and_replace(
        self,
        filter: dict[str, Any],  # pylint: disable=redefined-builtin  # nome do parâmetro no pymongo (o agno passa por nome)
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

    async def find_one_and_update(
        self,
        filter: dict[str, Any],  # pylint: disable=redefined-builtin  # nome do parâmetro no pymongo
        update: dict[str, Any],
        *,
        upsert: bool = False,
        return_document: bool = ReturnDocument.BEFORE,
    ) -> dict[str, Any] | None:
        """Só ``$inc`` (o contador da revisão da config, FA-05); sem casar e com ``upsert``, cria pelo filtro."""
        if set(update) != {"$inc"}:
            raise NotImplementedError(f"update não suportado pelo fake: {sorted(update)}")
        self._use(filter)
        found = next((d for d in self.docs if _matches(d, filter)), None)
        if found is None and not upsert:
            return None
        before = copy.deepcopy(found)
        if found is None:
            found = {"_id": ObjectId(), **{k: v for k, v in filter.items() if not isinstance(v, dict)}}
            self.docs.append(found)
        for key, amount in update["$inc"].items():
            found[key] = found.get(key, 0) + amount
        return copy.deepcopy(found) if return_document == ReturnDocument.AFTER else before

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

    async def index_information(self) -> dict[str, dict[str, Any]]:
        """Como no pymongo: nome → ``{"key": [(campo, direção)], "unique": ...}``; coleção que não existe
        no servidor (sem documento nem índice) não tem índice, nem o ``_id_``."""
        self._use()
        if not (self.docs or self.indexes):
            return {}
        info: dict[str, dict[str, Any]] = {"_id_": {"key": [("_id", 1)]}}
        for index in self.indexes:
            name = index["name"] or "_".join(f"{k}_{d}" for k, d in index["keys"])
            info[name] = {"key": list(index["keys"]), **({"unique": True} if index["unique"] else {})}
        return info

    async def drop(self) -> None:
        """Apaga documentos e índices: a coleção deixa de existir no servidor."""
        self._use()
        self.docs.clear()
        self.indexes.clear()
        self.unique_keys.clear()

    async def insert_one(self, doc: dict[str, Any]) -> None:
        """Como o pymongo: ``_id`` gerado no documento passado; repetido no índice único = ``DuplicateKeyError``."""
        self._use()
        doc.setdefault("_id", ObjectId())
        self._refuse_duplicate(doc)
        self.docs.append(copy.deepcopy(doc))

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


class _BoundCollections:
    """``client[db]``: as coleções do cliente (criadas no primeiro acesso, se o dict for um ``defaultdict``),
    cada uma amarrada ao cliente para recusar uso depois do ``close``. O resto do dict (``get``, ``items``...)
    é o do dict. Uma coleção pertence a um cliente só: o vínculo vale para o último cliente que a acessou."""

    def __init__(self, client: FakeMongoClient) -> None:
        self._client = client

    def __getitem__(self, collection_name: str) -> FakeMongoCollection:
        collection = self._client.collections[collection_name]
        collection.client = self._client
        return collection

    def __getattr__(self, name: str) -> Any:
        return getattr(self._client.collections, name)


class FakeMongoClient:
    """``client[db][collection]``: qualquer database expõe as mesmas coleções.

    Também serve de cliente do app (F3-09b: container, repositórios, diário e health no mesmo cliente):
    ``admin.command("ping")`` responde ok e ``close`` é corrotina, registrada em ``closes`` só quando
    aguardada (``True`` = na thread do event loop), como no ``AsyncMongoClient``. Depois do ``close`` toda
    operação nas coleções entregues por ele levanta ``InvalidOperation``, como o pymongo (um cliente fechado
    não volta a funcionar): uso depois do fechamento, no ciclo de vida, vira teste vermelho.
    """

    def __init__(self, collections: dict[str, FakeMongoCollection]) -> None:
        self.collections = collections
        self.closes: list[bool] = []

    def __getitem__(self, database_name: str) -> _BoundCollections:
        return _BoundCollections(self)

    def refuse_if_closed(self) -> None:
        if self.closes:
            raise InvalidOperation("Cannot use AsyncMongoClient after close")

    @property
    def admin(self) -> FakeMongoClient:
        return self

    async def command(self, name: str) -> dict[str, int]:
        self.refuse_if_closed()
        if name != "ping":
            raise NotImplementedError(f"comando {name!r} fora do fake")
        return {"ok": 1}

    async def close(self) -> None:
        self.closes.append(running_on_event_loop())


class FakeAsyncMongoCollection(FakeMongoCollection):
    """Coleção do ``AsyncMongoClient`` do pymongo: ``aggregate`` é corrotina e o cursor sai do ``await``
    (``await (await collection.aggregate(p)).to_list(...)``); o resto da API é a mesma do motor."""

    async def aggregate(self, pipeline: list[dict[str, Any]]) -> FakeAsyncCursor:  # type: ignore[override]
        return super().aggregate(pipeline)


class FakeMotorDatabase:
    """``database[collection]``: a coleção nasce no primeiro acesso, como no Mongo."""

    def __init__(self, client: _FakeClient) -> None:
        self._client = client
        self.collections: dict[str, FakeMongoCollection] = {}

    def __getitem__(self, collection_name: str) -> FakeMongoCollection:
        if collection_name not in self.collections:
            collection = self._client.collection_type()
            collection.client = self._client
            self.collections[collection_name] = collection
        return self.collections[collection_name]

    async def list_collection_names(self) -> list[str]:
        """Só as coleções que existiriam no servidor: com documento ou índice."""
        self._client.refuse_if_closed()
        return [name for name, collection in self.collections.items() if collection.docs or collection.indexes]

    async def command(self, name: str) -> dict[str, int]:
        """Só o ``ping`` (``client.admin.command("ping")`` do startup e do health)."""
        self._client.refuse_if_closed()
        if name != "ping":
            raise NotImplementedError(f"comando {name!r} fora do fake")
        return {"ok": 1}


class _FakeClient:
    """Um database por nome (``client[db][collection]``); depois do ``close`` toda operação levanta
    ``InvalidOperation``, como o pymongo."""

    collection_type: type[FakeMongoCollection] = FakeMongoCollection

    def __init__(self) -> None:
        self.databases: dict[str, FakeMotorDatabase] = {}
        self.closes: list[bool] = []
        """Por ``close``: ``True`` se rodou na thread do event loop."""

    def __getitem__(self, database_name: str) -> FakeMotorDatabase:
        if database_name not in self.databases:
            self.databases[database_name] = FakeMotorDatabase(self)
        return self.databases[database_name]

    @property
    def admin(self) -> FakeMotorDatabase:
        return self["admin"]

    def refuse_if_closed(self) -> None:
        if self.closes:
            raise InvalidOperation("Cannot use AsyncMongoClient after close")


class FakeMotorClient(_FakeClient):
    """Cliente motor em memória.

    O nome da classe tem ``Motor`` de propósito: o ``AsyncMongoDb`` do agno reconhece o tipo do
    cliente injetado pelo nome da classe (``_detect_client_type``). ``close`` é síncrono, como no
    motor, e registra em ``closes`` se rodou na thread do event loop (I/O síncrono no loop).
    """

    def close(self) -> None:
        self.closes.append(running_on_event_loop())


class FakeAsyncMongoClient(_FakeClient):
    """Cliente ``AsyncMongoClient`` (pymongo nativo) em memória (F3-09).

    O nome da classe tem ``AsyncMongo`` de propósito: é por ele que o ``_detect_client_type`` do agno
    3.1.12 o reconhece como o cliente nativo. ``aggregate`` é corrotina, como no driver. ``close`` é
    corrotina: só registra quando aguardado (um ``close()`` sem ``await``, ou chamado numa thread, não
    fecha nada, como no driver).
    """

    collection_type = FakeAsyncMongoCollection

    async def close(self) -> None:
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

    async def close(self) -> None:
        """Nada a fechar (corrotina, como no ``AsyncMongoClient``)."""


@dataclass
class SyncPymongoCalls:
    """O que o ``trap_sync_pymongo`` viu: cada item diz se rodou na thread do event loop."""

    operations: list[tuple[str, bool]] = field(default_factory=list)
    """``(operação, no event loop)`` de cada operação do pymongo síncrono (``find``, ``createIndexes``...)."""
    closes: list[bool] = field(default_factory=list)
    """Um item por ``MongoClient.close``."""
    async_operations: list[str] = field(default_factory=list)
    """Operações do ``AsyncMongoClient`` nativo (I/O async no loop, sem bloqueio), na ordem (F3-09)."""
    async_closes: int = 0
    """``AsyncMongoClient.close`` aguardados."""

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

    O ``AsyncMongoClient`` nativo (F3-09) não passa pelo pymongo síncrono: a escolha de servidor dele
    vai para ``async_operations`` e falha do mesmo jeito; o ``close`` aguardado conta em ``async_closes``.
    """
    calls = SyncPymongoCalls()
    original_close = SyncMongoClient.close
    original_async_close = AsyncMongoClient.close

    def select_server(self: Any, server_selector: Any, session: Any, operation: Any, *_: Any, **__: Any) -> Any:
        calls.operations.append((str(getattr(operation, "value", operation)), running_on_event_loop()))
        raise ServerSelectionTimeoutError("trap_sync_pymongo: sem servidor (detector)")

    def close(self: Any) -> None:
        calls.closes.append(running_on_event_loop())
        original_close(self)

    async def async_select_server(self: Any, selector: Any, session: Any, operation: Any, *_: Any, **__: Any) -> Any:
        calls.async_operations.append(str(getattr(operation, "value", operation)))
        raise ServerSelectionTimeoutError("trap_sync_pymongo: sem servidor (detector)")

    async def async_close(self: Any) -> None:
        calls.async_closes += 1
        await original_async_close(self)

    monkeypatch.setattr(SyncMongoClient, "_select_server", select_server)
    monkeypatch.setattr(SyncMongoClient, "close", close)
    monkeypatch.setattr(AsyncMongoClient, "_select_server", async_select_server)
    monkeypatch.setattr(AsyncMongoClient, "close", async_close)
    return calls
