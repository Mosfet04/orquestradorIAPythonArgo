"""Fidelidade da coleção Mongo em memória (``tests/fakes/mongo.py``) ao Mongo real (F2-06 R5, F2-08).

A mesma suíte roda contra a ``FakeMongoCollection`` (pelo ``FakeMotorClient``) e, só com
``MONGO_LIVE_URL`` (marker ``live``), contra uma coleção de um Mongo de verdade pelo motor. Cobre o
que os repositórios e o ``AsyncMongoDb`` do agno usam e em que um fake costuma divergir: ordenação
com campo ausente, igualdade BSON de booleano, índice único (``BulkWriteError`` 11000 com
``ordered=False``, ``DuplicateKeyError`` no upsert), ``find_one(sort=)``, ``None``/``$exists``/``$or``,
campo de lista, projeção e paginação.
"""

from __future__ import annotations

from typing import Any

import pytest
from pymongo import ReturnDocument
from pymongo.errors import BulkWriteError, DuplicateKeyError

from tests.fakes.config_stores import LIVE_BACKEND, live_mongo_for
from tests.fakes.mongo import FakeMotorClient

DUPLICATE_KEY = 11000


@pytest.fixture(params=["fake", pytest.param(LIVE_BACKEND, marks=pytest.mark.live)])
def collection(request: pytest.FixtureRequest) -> Any:
    """Coleção vazia: a fake ou uma do banco efêmero no Mongo real (API do motor nas duas)."""
    live = live_mongo_for(request)
    if live is None:
        return FakeMotorClient()["fidelidade"]["documentos"]
    return live.motor()[live.database]["documentos"]


async def _keys(cursor: Any) -> list[str]:
    return [doc["k"] async for doc in cursor]


async def test_ordenacao_poe_o_campo_ausente_antes_como_null(collection: Any) -> None:
    await collection.insert_many(
        [
            {"k": "lote-2", "_batch": 2, "_order": 0},
            {"k": "antigo-1", "_order": 1},
            {"k": "lote-1", "_batch": 1, "_order": 0},
            {"k": "antigo-0", "_order": 0},
        ]
    )

    ascending = await _keys(collection.find({}).sort([("_batch", 1), ("_order", 1)]))

    assert ascending == ["antigo-0", "antigo-1", "lote-1", "lote-2"]


async def test_booleano_so_casa_com_booleano_e_numero_so_com_numero(collection: Any) -> None:
    await collection.insert_many(
        [{"k": "true", "active": True}, {"k": "um", "active": 1}, {"k": "false", "active": False},
         {"k": "zero", "active": 0}, {"k": "sem"}]
    )

    assert await _keys(collection.find({"active": True})) == ["true"]
    assert await _keys(collection.find({"active": 1})) == ["um"]
    assert await _keys(collection.find({"active": False})) == ["false"]
    assert await _keys(collection.find({"active": 0})) == ["zero"]
    assert await _keys(collection.find({"active": {"$in": [True, 0]}})) == ["true", "zero"]


async def test_insert_many_nao_ordenado_grava_os_demais_e_levanta_bulk_write_error_11000(collection: Any) -> None:
    await collection.create_index([("id", 1)], name="idx_node_id", unique=True)
    await collection.insert_many([{"id": "r1", "k": "primeiro"}])
    docs = [{"id": "r2", "k": "novo"}, {"id": "r1", "k": "repetido"}, {"id": "r3", "k": "outro"},
            {"id": "r2", "k": "repetido-no-lote"}]

    with pytest.raises(BulkWriteError) as raised:
        await collection.insert_many(docs, ordered=False)

    errors = raised.value.details["writeErrors"]
    assert sorted((error["index"], error["code"]) for error in errors) == [(1, DUPLICATE_KEY), (3, DUPLICATE_KEY)]
    assert raised.value.details["nInserted"] == 2
    assert all("_id" in doc for doc in docs)  # o driver gera o _id no documento passado
    assert sorted(await _keys(collection.find({}))) == ["novo", "outro", "primeiro"]


async def test_insert_many_ordenado_para_no_primeiro_repetido(collection: Any) -> None:
    await collection.create_index([("id", 1)], name="idx_node_id", unique=True)

    with pytest.raises(BulkWriteError) as raised:
        await collection.insert_many([{"id": "a", "k": "a"}, {"id": "a", "k": "repetido"}, {"id": "b", "k": "b"}])

    assert [(e["index"], e["code"]) for e in raised.value.details["writeErrors"]] == [(1, DUPLICATE_KEY)]
    assert await _keys(collection.find({})) == ["a"]


async def test_find_one_com_sort_devolve_o_primeiro_da_ordem(collection: Any) -> None:
    await collection.insert_many(
        [{"id": "x", "k": "segundo", "_batch": 2}, {"id": "x", "k": "primeiro", "_batch": 1}, {"id": "y", "k": "y"}]
    )

    first = await collection.find_one({"id": "x"}, sort=[("_batch", 1)])
    last = await collection.find_one({"id": "x"}, sort=[("_batch", -1)])

    assert (first["k"], last["k"]) == ("primeiro", "segundo")
    assert await collection.find_one({"id": "nenhum"}, sort=[("_batch", 1)]) is None


async def test_count_documents_com_limite_e_in(collection: Any) -> None:
    await collection.insert_many([{"k": "a", "doc": "m"}, {"k": "b", "doc": "m"}, {"k": "c", "doc": "o"}])

    assert await collection.count_documents({"doc": "m"}) == 2
    assert await collection.count_documents({"doc": "m"}, limit=1) == 1
    assert await collection.count_documents({"doc": "x"}, limit=1) == 0
    assert await _keys(collection.find({"k": {"$in": ["c", "a", "z"]}})) == ["a", "c"]


async def test_none_casa_com_campo_nulo_ou_ausente_e_exists_so_com_o_ausente(collection: Any) -> None:
    await collection.insert_many([{"k": "ana", "user_id": "ana"}, {"k": "nulo", "user_id": None}, {"k": "sem"}])

    assert await _keys(collection.find({"user_id": None})) == ["nulo", "sem"]
    assert await _keys(collection.find({"user_id": {"$exists": False}})) == ["sem"]
    assert await _keys(collection.find({"user_id": {"$exists": True}})) == ["ana", "nulo"]
    either = {"$or": [{"user_id": "ana"}, {"user_id": {"$exists": False}}]}
    assert await _keys(collection.find(either)) == ["ana", "sem"]


async def test_campo_de_lista_casa_por_item_e_distinct_conta_item_a_item(collection: Any) -> None:
    await collection.insert_many(
        [
            {"k": "a", "topics": ["idioma", "comida"], "user_id": "ana"},
            {"k": "b", "topics": ["comida"], "user_id": "bia"},
            {"k": "c", "topics": None, "user_id": "ana"},
        ]
    )

    assert await _keys(collection.find({"topics": {"$in": ["idioma"]}})) == ["a"]
    assert await _keys(collection.find({"topics": "comida"})) == ["a", "b"]
    assert sorted(t for t in await collection.distinct("topics", {}) if t) == ["comida", "idioma"]
    assert sorted(t for t in await collection.distinct("topics", {"user_id": "bia"}) if t) == ["comida"]


async def test_projecao_de_inclusao_traz_o_campo_e_o_id(collection: Any) -> None:
    await collection.insert_many([{"k": "a", "user_id": "ana", "runs": [1, 2]}])

    doc = await collection.find_one({"k": "a"}, {"user_id": 1})

    assert set(doc) == {"_id", "user_id"} and doc["user_id"] == "ana"


async def test_sort_skip_limit_e_to_list(collection: Any) -> None:
    await collection.insert_many([{"k": name, "n": n} for n, name in enumerate(["a", "b", "c", "d", "e"])])

    page = await collection.find({}).sort([("n", -1)]).skip(1).limit(2).to_list(length=None)

    assert [doc["k"] for doc in page] == ["d", "c"]


async def test_replace_one_com_upsert_insere_e_depois_troca_mantendo_o_id(collection: Any) -> None:
    inserted = await collection.replace_one({"memory_id": "m1"}, {"memory_id": "m1", "memory": "v1"}, upsert=True)
    replaced = await collection.replace_one({"memory_id": "m1"}, {"memory_id": "m1", "memory": "v2"}, upsert=True)
    missing = await collection.replace_one({"memory_id": "m2"}, {"memory_id": "m2", "memory": "x"})

    assert inserted.upserted_id is not None and inserted.matched_count == 0
    assert replaced.upserted_id is None and replaced.matched_count == 1
    assert missing.upserted_id is None and missing.matched_count == 0
    docs = await collection.find({}).to_list(length=None)
    assert [(d["_id"], d["memory"]) for d in docs] == [(inserted.upserted_id, "v2")]


async def test_find_one_and_replace_com_upsert_devolve_o_documento_novo(collection: Any) -> None:
    created = await collection.find_one_and_replace(
        filter={"session_id": "s1"},
        replacement={"session_id": "s1", "user_id": "ana", "runs": []},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )
    updated = await collection.find_one_and_replace(
        filter={"session_id": "s1"},
        replacement={"session_id": "s1", "user_id": "ana", "runs": [1]},
        upsert=True,
        return_document=ReturnDocument.AFTER,
    )

    assert created["runs"] == [] and updated["runs"] == [1]
    assert updated["_id"] == created["_id"]
    assert await collection.count_documents({}) == 1


async def test_upsert_que_nao_casa_o_filtro_mas_repete_a_chave_unica_levanta_duplicate_key(collection: Any) -> None:
    """O ``upsert_session`` do agno conta com isto para não deixar um usuário trocar a sessão de outro."""
    await collection.create_index([("session_id", 1)], unique=True)
    await collection.insert_many([{"session_id": "s1", "user_id": "ana", "k": "da-ana"}])
    not_mine = {"session_id": "s1", "$or": [{"user_id": "bia"}, {"user_id": None}, {"user_id": {"$exists": False}}]}

    with pytest.raises(DuplicateKeyError) as raised:
        await collection.find_one_and_replace(
            filter=not_mine,
            replacement={"session_id": "s1", "user_id": "bia", "k": "da-bia"},
            upsert=True,
            return_document=ReturnDocument.AFTER,
        )

    assert raised.value.code == DUPLICATE_KEY
    assert await _keys(collection.find({})) == ["da-ana"]


async def test_delete_one_e_delete_many_contam_o_que_apagaram(collection: Any) -> None:
    await collection.insert_many([{"k": "a", "g": 1}, {"k": "b", "g": 1}, {"k": "c", "g": 2}])

    one = await collection.delete_one({"g": 1})
    none = await collection.delete_one({"g": 9})
    many = await collection.delete_many({"k": {"$in": ["b", "c"]}})

    assert (one.deleted_count, none.deleted_count, many.deleted_count) == (1, 0, 2)
    assert await collection.count_documents({}) == 0


async def test_ne_none_so_casa_com_campo_presente_e_nao_nulo(collection: Any) -> None:
    await collection.insert_many([{"k": "ana", "user_id": "ana"}, {"k": "nulo", "user_id": None}, {"k": "sem"}])

    assert await _keys(collection.find({"user_id": {"$ne": None}})) == ["ana"]
    assert await _keys(collection.find({"user_id": {"$ne": "ana"}})) == ["nulo", "sem"]


async def test_aggregate_das_estatisticas_de_memoria_por_usuario(collection: Any) -> None:
    """O pipeline do ``get_user_memory_stats`` do agno 2.5.8 (``GET /user_memory_stats``)."""
    await collection.insert_many(
        [
            {"user_id": "ana", "updated_at": 10},
            {"user_id": "bia", "updated_at": 30},
            {"user_id": "ana", "updated_at": 20},
            {"user_id": None, "updated_at": 99},
            {"updated_at": 98},
        ]
    )
    pipeline: list[dict[str, Any]] = [
        {"$match": {"user_id": {"$ne": None}}},
        {
            "$group": {
                "_id": "$user_id",
                "total_memories": {"$sum": 1},
                "last_memory_updated_at": {"$max": "$updated_at"},
            }
        },
        {"$sort": {"last_memory_updated_at": -1}},
    ]

    total = await collection.aggregate([*pipeline, {"$count": "total"}]).to_list(length=1)
    stats = await collection.aggregate(pipeline).to_list(length=None)
    page = await collection.aggregate([*pipeline, {"$skip": 1}, {"$limit": 1}]).to_list(length=None)

    assert total == [{"total": 2}]
    assert stats == [
        {"_id": "bia", "total_memories": 1, "last_memory_updated_at": 30},
        {"_id": "ana", "total_memories": 2, "last_memory_updated_at": 20},
    ]
    assert [row["_id"] for row in page] == ["ana"]
