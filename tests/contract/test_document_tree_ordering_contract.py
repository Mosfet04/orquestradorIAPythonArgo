"""Contrato do ``IDocumentTreeRepository``: memória e Mongo (coleção em memória de ``tests/fakes/mongo.py``).

Quem consome a árvore (``HierarchicalSearchTool``, indexação) depende de raízes e filhos voltarem
na ordem em que foram salvos. Semântica (pendência da F0-03, definida no F2-06):
- dentro de um lote de ``save_nodes``, a ordem da lista;
- entre lotes, a ordem de gravação: os nós de um lote anterior vêm antes dos de um lote posterior;
- id de nó repetido (no mesmo lote ou contra um lote anterior): vence o primeiro gravado; o
  repetido é ignorado (log de erro) e os demais nós do lote são gravados, sem levantar.
O Mongo garante o id único pelo índice ``idx_node_id`` (``ensure_indexes``); a coleção fake o
emula quando o índice é criado.
"""

from __future__ import annotations

import pytest

from src.domain.entities.document_node import DocumentNode
from src.domain.ports.document_tree_repository_port import IDocumentTreeRepository
from src.infrastructure.repositories import mongo_base
from src.infrastructure.repositories.mongo_document_tree_repository import MongoDocumentTreeRepository
from tests.fakes import FakeMongoClient, FakeMongoCollection, InMemoryDocumentTreeRepository, RecordingLogger


@pytest.fixture(params=["memory", "mongo"])
async def tree_repo(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> IDocumentTreeRepository:
    if request.param == "memory":
        return InMemoryDocumentTreeRepository()
    client = FakeMongoClient({"document_tree": FakeMongoCollection()})
    monkeypatch.setattr(mongo_base.MongoClientFactory, "get_client", classmethod(lambda cls, _conn: client))
    repo = MongoDocumentTreeRepository(connection_string="mongodb://mongo.invalid", logger=RecordingLogger())
    await repo.ensure_indexes()
    return repo


def _node(
    node_id: str, *, level: int, parent_id: str | None = None, doc: str = "manual", title: str | None = None
) -> DocumentNode:
    return DocumentNode(
        id=node_id, doc_name=doc, level=level, title=title or node_id, content="c", parent_id=parent_id
    )


def _tree(doc: str) -> list[DocumentNode]:
    root = DocumentNode(id=f"{doc}-0", doc_name=doc, level=0, title="Raiz", content="r", children_ids=[f"{doc}-1"])
    child = DocumentNode(id=f"{doc}-1", doc_name=doc, level=1, title="Filho", content="f", parent_id=f"{doc}-0")
    return [root, child]


def _ids(nodes: list[DocumentNode]) -> list[str]:
    return [n.id for n in nodes]


async def test_tree_repo_salva_e_navega_pela_arvore(tree_repo):
    assert await tree_repo.exists("manual") is False

    await tree_repo.save_nodes(_tree("manual") + _tree("outro"))

    assert await tree_repo.exists("manual") is True
    assert _ids(await tree_repo.get_root_nodes("manual")) == ["manual-0"]
    assert _ids(await tree_repo.get_children("manual-0")) == ["manual-1"]
    assert (await tree_repo.get_node("manual-1")).title == "Filho"
    assert await tree_repo.get_node("x") is None


async def test_tree_repo_preserva_embedding_e_resumo(tree_repo):
    nodes = _tree("manual")
    nodes[0].summary = "resumo"
    nodes[0].embedding = [0.1, 0.2]

    await tree_repo.save_nodes(nodes)
    (root,) = await tree_repo.get_root_nodes("manual")

    assert (root.summary, root.embedding, root.children_ids) == ("resumo", [0.1, 0.2], ["manual-1"])


async def test_tree_repo_devolve_raizes_e_filhos_na_ordem_em_que_foram_salvos(tree_repo):
    nodes = [
        _node("r2", level=0),
        _node("r1", level=0),
        _node("c3", level=1, parent_id="r1"),
        _node("c1", level=1, parent_id="r1"),
        _node("c2", level=1, parent_id="r1"),
    ]

    await tree_repo.save_nodes(nodes)

    assert _ids(await tree_repo.get_root_nodes("manual")) == ["r2", "r1"]
    assert _ids(await tree_repo.get_children("r1")) == ["c3", "c1", "c2"]


async def test_tree_repo_salvar_lista_vazia_nao_marca_documento_como_existente(tree_repo):
    await tree_repo.save_nodes([])

    assert await tree_repo.exists("manual") is False
    assert await tree_repo.get_root_nodes("manual") == []
    assert await tree_repo.get_children("qualquer") == []


async def test_tree_repo_get_root_nodes_ignora_outro_documento_e_niveis_internos(tree_repo):
    await tree_repo.save_nodes([_node("a0", level=0, doc="a"), _node("a1", level=1, parent_id="a0", doc="a")])
    await tree_repo.save_nodes([_node("b0", level=0, doc="b")])

    assert _ids(await tree_repo.get_root_nodes("a")) == ["a0"]
    assert _ids(await tree_repo.get_root_nodes("b")) == ["b0"]
    assert await tree_repo.get_root_nodes("inexistente") == []


async def test_tree_repo_entre_lotes_o_lote_anterior_vem_antes(tree_repo):
    """No Mongo, ``_order`` recomeça em 0 a cada lote: sem a ordem do lote, os lotes se intercalariam."""
    await tree_repo.save_nodes([_node("r1", level=0), _node("r2", level=0), _node("c1", level=1, parent_id="r1")])
    await tree_repo.save_nodes([_node("r3", level=0), _node("c2", level=1, parent_id="r1")])
    await tree_repo.save_nodes([_node("r0", level=0)])

    assert _ids(await tree_repo.get_root_nodes("manual")) == ["r1", "r2", "r3", "r0"]
    assert _ids(await tree_repo.get_children("r1")) == ["c1", "c2"]


async def test_tree_repo_id_repetido_no_mesmo_lote_vence_o_primeiro_e_grava_os_demais(tree_repo):
    await tree_repo.save_nodes(
        [_node("r1", level=0, title="primeiro"), _node("r2", level=0), _node("r1", level=0, title="repetido")]
    )

    roots = await tree_repo.get_root_nodes("manual")

    assert [(n.id, n.title) for n in roots] == [("r1", "primeiro"), ("r2", "r2")]
    assert (await tree_repo.get_node("r1")).title == "primeiro"


async def test_tree_repo_id_repetido_entre_lotes_vence_o_primeiro_gravado(tree_repo):
    await tree_repo.save_nodes([_node("r1", level=0, title="primeiro")])
    await tree_repo.save_nodes([_node("r1", level=0, title="repetido"), _node("r2", level=0)])

    roots = await tree_repo.get_root_nodes("manual")

    assert [(n.id, n.title) for n in roots] == [("r1", "primeiro"), ("r2", "r2")]
    assert (await tree_repo.get_node("r1")).title == "primeiro"
