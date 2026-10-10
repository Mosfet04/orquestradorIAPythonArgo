"""``MongoDocumentTreeRepository`` (F2-06): id repetido vira log sem levantar; outra falha de escrita sobe;
documento antigo (sem ``_batch``) continua legível e na ordem de antes. O contrato de ordem e de id
repetido está em ``tests/contract/test_document_tree_ordering_contract.py``."""

from __future__ import annotations

from typing import Any

import pytest
from pymongo.errors import BulkWriteError

from src.domain.entities.document_node import DocumentNode
from src.infrastructure.repositories import mongo_base
from src.infrastructure.repositories.mongo_document_tree_repository import MongoDocumentTreeRepository
from tests.fakes import FakeMongoClient, FakeMongoCollection, RecordingLogger


def _repo(
    monkeypatch: pytest.MonkeyPatch, collection: FakeMongoCollection, logger: RecordingLogger
) -> MongoDocumentTreeRepository:
    client = FakeMongoClient({"document_tree": collection})
    monkeypatch.setattr(mongo_base.MongoClientFactory, "get_client", classmethod(lambda cls, _conn: client))
    return MongoDocumentTreeRepository(connection_string="mongodb://mongo.invalid", logger=logger)


def _node(node_id: str, level: int = 0) -> DocumentNode:
    return DocumentNode(id=node_id, doc_name="manual", level=level, title=node_id, content="c")


async def test_ensure_indexes_cria_o_indice_unico_do_id_do_no(monkeypatch: pytest.MonkeyPatch) -> None:
    collection = FakeMongoCollection()
    await _repo(monkeypatch, collection, RecordingLogger()).ensure_indexes()

    assert {"keys": [("id", 1)], "name": "idx_node_id", "unique": True} in collection.indexes


async def test_id_repetido_loga_os_ids_e_quantos_foram_gravados_sem_levantar(monkeypatch: pytest.MonkeyPatch) -> None:
    collection = FakeMongoCollection()
    logger = RecordingLogger()
    repo = _repo(monkeypatch, collection, logger)
    await repo.ensure_indexes()
    await repo.save_nodes([_node("a")])

    await repo.save_nodes([_node("a"), _node("b"), _node("b")])

    assert [d["id"] for d in collection.docs] == ["a", "b"]
    assert [(r.level, r.message, r.context) for r in logger.records if r.level == "error"] == [
        ("error", "Nós com id repetido ignorados; vale o primeiro gravado", {"node_ids": ["a", "b"], "saved": 1})
    ]


class _FailingCollection(FakeMongoCollection):
    def __init__(self, details: dict[str, Any]) -> None:
        super().__init__()
        self._details = details

    async def insert_many(self, docs: list[dict[str, Any]], *, ordered: bool = True) -> None:
        raise BulkWriteError(self._details)


@pytest.mark.parametrize(
    "details",
    [
        {"writeErrors": [{"index": 0, "code": 121, "errmsg": "Document failed validation"}]},
        {"writeErrors": [{"index": 0, "code": 11000}, {"index": 1, "code": 121}]},
        {"writeErrors": [{"index": 0, "code": 11000}], "writeConcernErrors": [{"code": 64}]},
        {"writeErrors": [], "writeConcernErrors": [{"code": 64}]},
    ],
    ids=["validacao", "repetido-e-validacao", "repetido-com-write-concern", "so-write-concern"],
)
async def test_falha_de_escrita_que_nao_e_so_id_repetido_sobe(
    monkeypatch: pytest.MonkeyPatch, details: dict[str, Any]
) -> None:
    logger = RecordingLogger()
    repo = _repo(monkeypatch, _FailingCollection(details), logger)

    with pytest.raises(BulkWriteError):
        await repo.save_nodes([_node("a"), _node("b")])

    assert [(r.message, r.context) for r in logger.records if r.level == "error"] == [
        ("Erro ao salvar nós", {"error_type": "BulkWriteError"})
    ]


async def test_documento_antigo_sem_batch_continua_legivel_e_vem_antes(monkeypatch: pytest.MonkeyPatch) -> None:
    """Árvore indexada antes do F2-06 (só ``_order``): lida na ordem de sempre, antes de um lote novo."""
    legacy = [
        {"id": "r2", "doc_name": "manual", "level": 0, "title": "r2", "content": "c", "_order": 1},
        {"id": "r1", "doc_name": "manual", "level": 0, "title": "r1", "content": "c", "_order": 0},
    ]
    repo = _repo(monkeypatch, FakeMongoCollection(legacy), RecordingLogger())
    await repo.save_nodes([_node("novo")])

    assert [n.id for n in await repo.get_root_nodes("manual")] == ["r1", "r2", "novo"]
    assert (await repo.get_node("r2")).title == "r2"
