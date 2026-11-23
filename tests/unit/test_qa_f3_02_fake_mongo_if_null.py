"""F3-02 (R1-1): o ``$ifNull`` do ``FakeMongoCollection`` só aceita a forma que o Mongo real avalia igual.

O fake só emula ``{"$ifNull": ["$campo_de_topo", default_literal]}``. Caminho pontilhado, default que é
expressão e campo sem ``$`` o Mongo avalia de outro jeito: aceitar em silêncio daria teste verde com
fake e vermelho no banco real. Aqui só o fake (``pytest.raises``); o caso positivo está no contrato
de fidelidade.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.fakes.mongo import FakeMotorClient


def _collection() -> Any:
    return FakeMotorClient()["f3-02"]["docs"]


async def _add_fields(expression: Any) -> list[dict[str, Any]]:
    collection = _collection()
    await collection.insert_one({"k": "a", "x": None, "nested": {"y": 1}, "texto": "$x"})
    return await collection.aggregate([{"$addFields": {"out": expression}}]).to_list(length=None)


@pytest.mark.parametrize(
    "expression",
    [
        pytest.param({"$ifNull": ["$nested.y", 0]}, id="caminho-pontilhado"),
        pytest.param({"$ifNull": ["$x", "$outro"]}, id="default-campo"),
        pytest.param({"$ifNull": ["$x", "$$NOW"]}, id="default-variavel"),
        pytest.param({"$ifNull": ["$x", ["$y"]]}, id="default-lista-com-expressao"),
        pytest.param({"$ifNull": ["$x", {"a": "$y"}]}, id="default-documento-com-expressao"),
        pytest.param({"$ifNull": ["$x", {"$add": [1, 2]}]}, id="default-operador"),
        pytest.param({"$ifNull": ["x", 0]}, id="campo-sem-cifrao"),
        pytest.param({"$ifNull": ["$$ROOT", 0]}, id="variavel-como-campo"),
        pytest.param({"$ifNull": ["$", 0]}, id="campo-vazio"),
        pytest.param({"$ifNull": [5, 0]}, id="campo-nao-string"),
        pytest.param({"$ifNull": []}, id="lista-vazia"),
        pytest.param({"$ifNull": ["$x"]}, id="lista-de-um"),
        pytest.param({"$ifNull": ["$x", 0, 1]}, id="lista-de-tres"),
        pytest.param({"$ifNull": "$x"}, id="argumento-nao-lista"),
        pytest.param({"$ifNull": ["$x", 0], "$other": 1}, id="expressao-com-outro-operador"),
        pytest.param({"$toString": "$x"}, id="outra-expressao"),
        pytest.param("$x", id="expressao-de-campo"),
        pytest.param(0, id="literal-nao-dict"),
    ],
)
async def test_if_null_fora_da_forma_suportada_falha_alto(expression: Any) -> None:
    with pytest.raises(NotImplementedError, match=r"FakeMongoCollection\.aggregate"):
        await _add_fields(expression)


async def test_if_null_aceita_default_literal_inclusive_string_sem_cifrao_e_tupla() -> None:
    collection = _collection()
    await collection.insert_many([{"k": "nulo", "x": None}, {"k": "ausente"}, {"k": "cheio", "x": 7}])
    pipeline = [
        {
            "$addFields": {
                "a": {"$ifNull": ["$x", "padrao"]},
                "b": {"$ifNull": ("$x", [1, 2])},
                "c": {"$ifNull": ["$x", {"literal": "ok"}]},
                "d": {"$ifNull": ["$x", 0]},
            }
        }
    ]

    rows = await collection.aggregate(pipeline).to_list(length=None)

    assert [(r["k"], r["a"], r["b"], r["c"], r["d"]) for r in rows] == [
        ("nulo", "padrao", [1, 2], {"literal": "ok"}, 0),
        ("ausente", "padrao", [1, 2], {"literal": "ok"}, 0),
        ("cheio", 7, 7, 7, 7),
    ]
