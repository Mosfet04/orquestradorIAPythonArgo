"""QA F3-03: lacunas do filtro ``user_id`` do RAG (``GuardedMongoVectorDb``), no nível do vector db.

Complementa ``test_f3_03_rag_user_filter.py`` (que cobre a montagem pelo ``AgentFactoryService``):
chamada posicional do ``prepare_doc`` como o 3.x permite, o dono nunca se perde no reparo, o reparo
repetido não muda nada e o relatório do reparo com falhas mistas. Driver Mongo em memória
(``tests/fakes/vector_mongo.py``); nenhum LLM nem rede.
"""

from __future__ import annotations

from typing import Any

import pytest
from agno.knowledge.document import Document
from agno.vectordb.mongodb import MongoDb as MongoVectorDb

from src.infrastructure.runtime.agno.rag_vector_db import GuardedMongoVectorDb
from tests.fakes.embedders import ScriptedEmbedder
from tests.fakes.vector_mongo import FakeVectorServer, vector_index

pytestmark = pytest.mark.unit

DB_URL = "mongodb://mongo.test.invalid:27017"
DB_NAME = "test_db"
COLLECTION = "rag_qa"
LEAK_MARKER = "valor-de-teste-do-provider"  # marcador de vazamento, não é segredo
USER_FILTER = {"type": "filter", "path": "user_id"}


class EmbeddingError(Exception):
    """No lugar do ``agno.exceptions.EmbeddingError`` do 3.x."""


def _vector_db(embedder: ScriptedEmbedder | None = None, dimensions: int = 8) -> GuardedMongoVectorDb:
    return GuardedMongoVectorDb(
        collection_name=COLLECTION,
        db_url=DB_URL,
        database=DB_NAME,
        embedder=embedder or ScriptedEmbedder(dimensions=dimensions),
        dimensions=dimensions,
    )


def _chunk(chunk_id: str, **extra: Any) -> dict[str, Any]:
    return {"_id": chunk_id, "name": "manual.md", "content": f"texto {chunk_id}", "content_hash": "h", **extra}


def _prepare_doc_3x(
    self: MongoVectorDb,
    content_hash: str,
    document: Document,
    filters: dict[str, Any] | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """Assinatura e documento do ``prepare_doc`` do agno 3.1.12 (``mongodb.py:1097-1127``)."""
    return {"_id": content_hash, "content": document.content, "embedding": document.embedding, "user_id": user_id}


def test_prepare_doc_aceita_o_user_id_posicional_e_grava_o_dono(
    fake_vector_mongo: FakeVectorServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O 3.x pode chamar ``prepare_doc(hash, doc, filters, user_id)``: nunca ``TypeError``."""
    monkeypatch.setattr(MongoVectorDb, "prepare_doc", _prepare_doc_3x)

    prepared = _vector_db().prepare_doc("h", Document(content="t", embedding=[0.5] * 8), None, "u9")

    assert prepared["user_id"] == "u9"


def test_prepare_doc_sem_dono_grava_user_id_nulo(
    fake_vector_mongo: FakeVectorServer,
) -> None:
    """No 2.5.8 (pai sem ``user_id``): chunk compartilhado = ``user_id: None``, com a chave presente."""
    prepared = _vector_db().prepare_doc("h", Document(content="t", embedding=[0.5] * 8), None)

    assert "user_id" in prepared and prepared["user_id"] is None


def test_prepare_doc_posicional_com_embedding_invalido_continua_value_error(
    fake_vector_mongo: FakeVectorServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(MongoVectorDb, "prepare_doc", _prepare_doc_3x)
    vector_db = _vector_db()

    with pytest.raises(ValueError):  # o agno só pula o documento com ValueError
        vector_db.prepare_doc("h", Document(content="t", embedding=[0.5] * 3), None, "u1")

    assert vector_db.rejected_chunks == 1


def test_reparo_preserva_o_dono_ao_recriar_o_indice_e_ao_reindexar_o_embedding(
    fake_vector_mongo: FakeVectorServer,
) -> None:
    owned_bad = _chunk("c1", embedding=[], user_id="u1")  # dono + embedding inválido
    owned_ok = _chunk("c2", embedding=[0.5] * 8, user_id="u2")
    shared = _chunk("c3", embedding=[0.5] * 8, user_id=None)
    legacy = _chunk("c4", embedding=[0.5] * 8)
    collection = fake_vector_mongo.seed(
        DB_NAME, COLLECTION, [owned_bad, owned_ok, shared, legacy], index_dimensions=8, index_user_filter=False
    )

    report = _vector_db().repair()

    assert (report.index_rebuilt, report.reembedded, report.failed, report.shared) == (True, 1, 0, 1)
    assert collection.docs["c1"]["user_id"] == "u1" and len(collection.docs["c1"]["embedding"]) == 8
    assert collection.docs["c2"] == owned_ok
    assert collection.docs["c3"] == shared
    assert collection.docs["c4"] == {**legacy, "user_id": None}


def test_reparo_repetido_nao_muda_nada_e_nao_conta_compartilhados(fake_vector_mongo: FakeVectorServer) -> None:
    legacy = _chunk("c1", embedding=[0.5] * 8)
    owned = _chunk("c2", embedding=[0.5] * 8, user_id="u1")
    collection = fake_vector_mongo.seed(
        DB_NAME, COLLECTION, [legacy, owned], index_dimensions=8, index_user_filter=False
    )
    first = _vector_db().repair()
    snapshot = {key: dict(doc) for key, doc in collection.docs.items()}
    collection.ops.clear()

    second = _vector_db().repair()

    assert (first.index_rebuilt, first.shared) == (True, 1)
    assert (second.index_rebuilt, second.shared, second.reembedded, second.failed) == (False, 0, 0, 0)
    assert second.error_type is None
    assert collection.docs == snapshot
    assert not [op for op, _ in collection.ops if op in {"create_search_index", "drop_search_index"}]


def test_indice_com_o_filtro_em_outro_campo_e_recriado(fake_vector_mongo: FakeVectorServer) -> None:
    index = vector_index(8, user_filter=False)
    index["latestDefinition"]["fields"].append({"type": "filter", "path": "name"})
    collection = fake_vector_mongo.seed(DB_NAME, COLLECTION, [], index_dimensions=8)
    collection.search_indexes[:] = [index]

    report = _vector_db().repair()

    assert report.index_rebuilt is True and report.previous_dimensions == 8
    [fields] = [i["latestDefinition"]["fields"] for i in collection.search_indexes]
    assert USER_FILTER in fields


def test_indice_com_o_filtro_e_dimensao_certa_nao_e_recriado(fake_vector_mongo: FakeVectorServer) -> None:
    collection = fake_vector_mongo.seed(DB_NAME, COLLECTION, [], index_dimensions=8)

    report = _vector_db().repair()

    assert report.index_rebuilt is False and collection.dropped_search_indexes == []


def test_indice_ausente_e_criado_com_o_filtro(fake_vector_mongo: FakeVectorServer) -> None:
    collection = fake_vector_mongo.seed(DB_NAME, COLLECTION, [_chunk("c1", embedding=[0.5] * 8)])

    report = _vector_db().repair()

    assert (report.previous_dimensions, report.index_rebuilt, report.shared) == (None, True, 1)
    [fields] = [i["latestDefinition"]["fields"] for i in collection.search_indexes]
    assert fields[-1] == USER_FILTER


def test_reparo_com_falhas_mistas_reindexa_os_bons_conta_as_falhas_e_so_expoe_o_tipo(
    fake_vector_mongo: FakeVectorServer,
) -> None:
    def script(text: str) -> int | None:
        if "ruim" in text:
            raise EmbeddingError(f"401 Unauthorized: chave {LEAK_MARKER} recusada")
        return None

    docs = [
        _chunk("c1", embedding=[], user_id="u1", content="texto bom"),
        _chunk("c2", embedding=[], user_id="u2", content="texto ruim"),
        _chunk("c3", embedding=[], content=""),  # sem texto: não há o que embutir
    ]
    collection = fake_vector_mongo.seed(DB_NAME, COLLECTION, docs, index_dimensions=8)

    report = _vector_db(ScriptedEmbedder(dimensions=8, size_for=script)).repair()

    assert (report.reembedded, report.failed) == (1, 2)
    assert report.error_type == "EmbeddingDimensionError"  # a última falha foi o chunk sem texto
    assert len(collection.docs["c1"]["embedding"]) == 8 and collection.docs["c1"]["user_id"] == "u1"
    assert collection.docs["c2"]["embedding"] == [] and collection.docs["c2"]["user_id"] == "u2"
    assert LEAK_MARKER not in repr(report)


def test_reparo_so_com_falha_do_embedder_expoe_o_tipo_da_excecao_sem_o_texto(
    fake_vector_mongo: FakeVectorServer,
) -> None:
    def fail(_text: str) -> int:
        raise EmbeddingError(f"chave {LEAK_MARKER}")

    fake_vector_mongo.seed(DB_NAME, COLLECTION, [_chunk("c1", embedding=[])], index_dimensions=8)

    report = _vector_db(ScriptedEmbedder(dimensions=8, size_for=fail)).repair()

    assert (report.reembedded, report.failed, report.error_type) == (0, 1, "EmbeddingError")
    assert LEAK_MARKER not in repr(report)
