"""Contrato das portas de ``src/domain/repositories`` e de ``IDocumentTreeRepository``.

Semântica tirada dos adapters Mongo (``src/infrastructure/repositories``).
"""

from __future__ import annotations

from collections.abc import Callable, Iterable

import pytest

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.document_node import DocumentNode
from src.domain.entities.team_config import TeamConfig
from src.domain.entities.tool import HttpMethod, Tool
from src.domain.ports.document_tree_repository_port import IDocumentTreeRepository
from src.domain.repositories.agent_config_repository import IAgentConfigRepository
from src.domain.repositories.team_config_repository import ITeamConfigRepository
from src.domain.repositories.tool_repository import IToolRepository
from tests.fakes import (
    InMemoryAgentConfigRepository,
    InMemoryDocumentTreeRepository,
    InMemoryTeamConfigRepository,
    InMemoryToolRepository,
)


def _agent(agent_id: str, *, active: bool = True) -> AgentConfig:
    return AgentConfig(
        id=agent_id, nome=agent_id, factory_ia_model="ollama", model="m", descricao="d", prompt="p", active=active
    )


def _team(team_id: str, *, active: bool = True) -> TeamConfig:
    return TeamConfig(id=team_id, nome=team_id, factory_ia_model="ollama", model="m", member_ids=["a"], active=active)


def _tool(tool_id: str, *, active: bool = True) -> Tool:
    return Tool(
        id=tool_id,
        name=tool_id,
        description="d",
        route="https://example.invalid",
        http_method=HttpMethod.GET,
        parameters=[],
        active=active,
    )


# ── IAgentConfigRepository ──────────────────────────────────────────


@pytest.fixture(params=[pytest.param(InMemoryAgentConfigRepository, id="memory")])
def make_agent_repo(request: pytest.FixtureRequest) -> Callable[[Iterable[AgentConfig]], IAgentConfigRepository]:
    return request.param


async def test_agent_repo_lista_so_ativos(make_agent_repo):
    repo = make_agent_repo([_agent("a"), _agent("b", active=False)])

    assert [c.id for c in await repo.get_active_agents()] == ["a"]


async def test_agent_repo_busca_por_id_inclusive_inativo(make_agent_repo):
    repo = make_agent_repo([_agent("b", active=False)])

    found = await repo.get_agent_by_id("b")

    assert (found.id, found.active) == ("b", False)


async def test_agent_repo_id_inexistente_levanta_value_error(make_agent_repo):
    repo = make_agent_repo([])

    with pytest.raises(ValueError, match="não encontrado"):
        await repo.get_agent_by_id("x")


async def test_agent_repo_leitura_devolve_copia_independente(make_agent_repo):
    repo = make_agent_repo([_agent("a")])

    (first,) = await repo.get_active_agents()
    first.prompt = "alterado"

    assert (await repo.get_agent_by_id("a")).prompt == "p"


# ── ITeamConfigRepository ───────────────────────────────────────────


@pytest.fixture(params=[pytest.param(InMemoryTeamConfigRepository, id="memory")])
def make_team_repo(request: pytest.FixtureRequest) -> Callable[[Iterable[TeamConfig]], ITeamConfigRepository]:
    return request.param


async def test_team_repo_lista_so_ativos(make_team_repo):
    repo = make_team_repo([_team("t1"), _team("t2", active=False)])

    assert [c.id for c in await repo.get_active_teams()] == ["t1"]


async def test_team_repo_id_inexistente_devolve_none(make_team_repo):
    repo = make_team_repo([_team("t1")])

    assert await repo.get_team_by_id("x") is None
    assert (await repo.get_team_by_id("t1")).id == "t1"


# ── IToolRepository ─────────────────────────────────────────────────


@pytest.fixture(params=[pytest.param(InMemoryToolRepository, id="memory")])
def make_tool_repo(request: pytest.FixtureRequest) -> Callable[[Iterable[Tool]], IToolRepository]:
    return request.param


async def test_tool_repo_por_ids_filtra_ids_e_ativos(make_tool_repo):
    repo = make_tool_repo([_tool("a"), _tool("b"), _tool("c", active=False)])

    found = await repo.get_tools_by_ids(["a", "c", "inexistente"])

    assert [t.id for t in found] == ["a"]
    assert await repo.get_tools_by_ids([]) == []


async def test_tool_repo_todas_ativas_e_busca_por_id(make_tool_repo):
    repo = make_tool_repo([_tool("a"), _tool("b", active=False)])

    assert [t.id for t in await repo.get_all_active_tools()] == ["a"]
    assert (await repo.get_tool_by_id("b")).id == "b"
    with pytest.raises(ValueError, match="não encontrada"):
        await repo.get_tool_by_id("x")


# ── IDocumentTreeRepository ─────────────────────────────────────────


@pytest.fixture(params=[pytest.param(InMemoryDocumentTreeRepository, id="memory")])
def make_tree_repo(request: pytest.FixtureRequest) -> Callable[[], IDocumentTreeRepository]:
    return request.param


def _tree(doc: str) -> list[DocumentNode]:
    root = DocumentNode(id=f"{doc}-0", doc_name=doc, level=0, title="Raiz", content="r", children_ids=[f"{doc}-1"])
    child = DocumentNode(id=f"{doc}-1", doc_name=doc, level=1, title="Filho", content="f", parent_id=f"{doc}-0")
    return [root, child]


async def test_tree_repo_salva_e_navega_pela_arvore(make_tree_repo):
    repo = make_tree_repo()
    assert await repo.exists("manual") is False

    await repo.save_nodes(_tree("manual") + _tree("outro"))

    assert await repo.exists("manual") is True
    assert [n.id for n in await repo.get_root_nodes("manual")] == ["manual-0"]
    assert [n.id for n in await repo.get_children("manual-0")] == ["manual-1"]
    assert (await repo.get_node("manual-1")).title == "Filho"
    assert await repo.get_node("x") is None


async def test_tree_repo_preserva_embedding_e_resumo(make_tree_repo):
    repo = make_tree_repo()
    nodes = _tree("manual")
    nodes[0].summary = "resumo"
    nodes[0].embedding = [0.1, 0.2]

    await repo.save_nodes(nodes)
    (root,) = await repo.get_root_nodes("manual")

    assert (root.summary, root.embedding) == ("resumo", [0.1, 0.2])
