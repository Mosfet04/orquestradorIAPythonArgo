"""Contrato de ordem do ``IDocumentTreeRepository`` (lacuna deixada pela suíte principal).

O adapter Mongo ordena por ``_order`` (posição do nó em ``save_nodes``); quem consome a
árvore (``HierarchicalSearchTool``, indexação) depende de raízes e filhos voltarem na
ordem em que foram salvos. Toda implementação precisa respeitar isso.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from src.domain.entities.document_node import DocumentNode
from src.domain.ports.document_tree_repository_port import IDocumentTreeRepository
from tests.fakes import InMemoryDocumentTreeRepository


@pytest.fixture(params=[pytest.param(InMemoryDocumentTreeRepository, id="memory")])
def make_tree_repo(request: pytest.FixtureRequest) -> Callable[[], IDocumentTreeRepository]:
    return request.param


def _node(node_id: str, *, level: int, parent_id: str | None = None, doc: str = "manual") -> DocumentNode:
    return DocumentNode(id=node_id, doc_name=doc, level=level, title=node_id, content="c", parent_id=parent_id)


async def test_tree_repo_devolve_raizes_e_filhos_na_ordem_em_que_foram_salvos(make_tree_repo):
    repo = make_tree_repo()
    nodes = [
        _node("r2", level=0),
        _node("r1", level=0),
        _node("c3", level=1, parent_id="r1"),
        _node("c1", level=1, parent_id="r1"),
        _node("c2", level=1, parent_id="r1"),
    ]

    await repo.save_nodes(nodes)

    assert [n.id for n in await repo.get_root_nodes("manual")] == ["r2", "r1"]
    assert [n.id for n in await repo.get_children("r1")] == ["c3", "c1", "c2"]


async def test_tree_repo_salvar_lista_vazia_nao_marca_documento_como_existente(make_tree_repo):
    repo = make_tree_repo()

    await repo.save_nodes([])

    assert await repo.exists("manual") is False
    assert await repo.get_root_nodes("manual") == []
    assert await repo.get_children("qualquer") == []


async def test_tree_repo_get_root_nodes_ignora_outro_documento_e_niveis_internos(make_tree_repo):
    repo = make_tree_repo()
    await repo.save_nodes([_node("a0", level=0, doc="a"), _node("a1", level=1, parent_id="a0", doc="a")])
    await repo.save_nodes([_node("b0", level=0, doc="b")])

    assert [n.id for n in await repo.get_root_nodes("a")] == ["a0"]
    assert [n.id for n in await repo.get_root_nodes("b")] == ["b0"]
    assert await repo.get_root_nodes("inexistente") == []
