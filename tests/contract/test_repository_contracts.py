"""Contrato das portas de config (``src/domain/repositories``): a mesma suíte para memória, Mongo e YAML.

As três portas (agentes, teams, tools) são o ``ConfigStore`` do F2-06; ``CONFIG_STORE=mongo|yaml``
escolhe a implementação no composition root. Mongo roda sobre a coleção em memória de
``tests/fakes/mongo.py`` (o Mongo real fica para ``live``) e YAML sobre um arquivo em ``tmp_path``.

Semântica comum (F2-06):
- ordem estável: a do backend (``_id`` crescente no Mongo, ordem no arquivo no YAML, ordem de
  inserção na memória);
- id repetido: as listagens de agentes e teams devolvem todos, na ordem estável (o caso de uso fica
  com o primeiro e loga o repetido, F1-10); as de tools devolvem uma por id, a primeira; a busca por
  id devolve o primeiro documento com o id;
- só entra na listagem o que tem ``active`` verdadeiro; a busca por id acha inativo também.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.rag_config import RagConfig, SearchStrategy
from src.domain.entities.team_config import TeamConfig
from src.domain.entities.tool import HttpMethod, ParameterType, Tool, ToolParameter
from tests.fakes.config_stores import ENTITY_BACKENDS, EntityBackend, MemoryBackend, entity_backend


@pytest.fixture(params=ENTITY_BACKENDS)
def backend(
    request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> MemoryBackend | EntityBackend:
    return entity_backend(request.param, monkeypatch, tmp_path)


def _agent(agent_id: str, *, active: bool = True, nome: str | None = None) -> AgentConfig:
    return AgentConfig(
        id=agent_id,
        nome=nome or agent_id,
        factory_ia_model="ollama",
        model="m",
        descricao="d",
        prompt="p",
        active=active,
        tools_ids=[],
    )


def _team(team_id: str, *, active: bool = True, nome: str | None = None) -> TeamConfig:
    return TeamConfig(
        id=team_id, nome=nome or team_id, factory_ia_model="ollama", model="m", member_ids=["a"], active=active
    )


def _tool(tool_id: str, *, active: bool = True, name: str | None = None) -> Tool:
    return Tool(
        id=tool_id,
        name=name or tool_id,
        description="d",
        route="https://example.invalid",
        http_method=HttpMethod.GET,
        parameters=[],
        active=active,
    )


# ── IAgentConfigRepository ──────────────────────────────────────────


async def test_agent_repo_lista_so_ativos(backend):
    repo = backend.agent_repo([_agent("a"), _agent("b", active=False)])

    assert [c.id for c in await repo.get_active_agents()] == ["a"]


async def test_agent_repo_busca_por_id_inclusive_inativo(backend):
    repo = backend.agent_repo([_agent("b", active=False)])

    found = await repo.get_agent_by_id("b")

    assert (found.id, found.active) == ("b", False)


async def test_agent_repo_id_inexistente_levanta_value_error(backend):
    repo = backend.agent_repo([])

    with pytest.raises(ValueError, match="não encontrado"):
        await repo.get_agent_by_id("x")


async def test_agent_repo_leitura_devolve_copia_independente(backend):
    repo = backend.agent_repo([_agent("a")])

    (first,) = await repo.get_active_agents()
    first.prompt = "alterado"
    first.tools_ids.append("t")

    again = await repo.get_agent_by_id("a")
    assert (again.prompt, again.tools_ids) == ("p", [])


async def test_agent_repo_ordem_estavel_com_id_repetido_e_busca_devolve_o_primeiro(backend):
    repo = backend.agent_repo([_agent("z"), _agent("dup", nome="primeiro"), _agent("a"), _agent("dup", nome="segundo")])

    listed = await repo.get_active_agents()

    assert [(c.id, c.nome) for c in listed] == [("z", "z"), ("dup", "primeiro"), ("a", "a"), ("dup", "segundo")]
    assert (await repo.get_agent_by_id("dup")).nome == "primeiro"


async def test_agent_repo_devolve_a_entidade_completa_igual_a_gravada(backend):
    """Mesmo mapper e mesma validação: todos os campos (inclusive RAG e os opcionais do F2-01) voltam iguais."""
    config = AgentConfig(
        id="completo",
        nome="Completo",
        factory_ia_model="openai_compatible",
        model="gpt-x",
        descricao=None,
        prompt="p",
        tools_ids=["t1", "t2"],
        rag_config=RagConfig(
            active=True,
            doc_name="manual.md",
            model="emb",
            factory_ia_model="ollama",
            search_strategy=SearchStrategy.HIERARCHICAL,
            model_params={"dimensions": 256},
        ),
        user_memory_active=True,
        summary_active=True,
        model_params={"temperature": 0.2},
        base_url="https://gw.example/v1",
        api_key_ref="env:GATEWAY_API_KEY",
    )
    repo = backend.agent_repo([config])

    assert await repo.get_active_agents() == [config]
    assert await repo.get_agent_by_id("completo") == config


# ── ITeamConfigRepository ───────────────────────────────────────────


async def test_team_repo_lista_so_ativos(backend):
    repo = backend.team_repo([_team("t1"), _team("t2", active=False)])

    assert [c.id for c in await repo.get_active_teams()] == ["t1"]


async def test_team_repo_id_inexistente_devolve_none(backend):
    repo = backend.team_repo([_team("t1")])

    assert await repo.get_team_by_id("x") is None
    assert (await repo.get_team_by_id("t1")).id == "t1"


async def test_team_repo_ordem_estavel_com_id_repetido_e_busca_devolve_o_primeiro(backend):
    repo = backend.team_repo([_team("dup", nome="primeiro"), _team("b"), _team("dup", nome="segundo")])

    listed = await repo.get_active_teams()

    assert [(c.id, c.nome) for c in listed] == [("dup", "primeiro"), ("b", "b"), ("dup", "segundo")]
    assert (await repo.get_team_by_id("dup")).nome == "primeiro"


async def test_team_repo_devolve_a_entidade_completa_igual_a_gravada(backend):
    config = TeamConfig(
        id="time",
        nome="Time",
        factory_ia_model="ollama",
        model="m",
        member_ids=["a", "b"],
        mode="coordinate",
        descricao="d",
        prompt="p",
        user_memory_active=False,
        summary_active=True,
        model_params={"options.num_ctx": 4096},
    )
    repo = backend.team_repo([config])

    assert await repo.get_active_teams() == [config]
    assert await repo.get_team_by_id("time") == config


# ── IToolRepository ─────────────────────────────────────────────────


async def test_tool_repo_por_ids_filtra_ids_e_ativos(backend):
    repo = backend.tool_repo([_tool("a"), _tool("b"), _tool("c", active=False)])

    found = await repo.get_tools_by_ids(["a", "c", "inexistente"])

    assert [t.id for t in found] == ["a"]
    assert await repo.get_tools_by_ids([]) == []


async def test_tool_repo_todas_ativas_e_busca_por_id(backend):
    repo = backend.tool_repo([_tool("a"), _tool("b", active=False)])

    assert [t.id for t in await repo.get_all_active_tools()] == ["a"]
    assert (await repo.get_tool_by_id("b")).id == "b"
    with pytest.raises(ValueError, match="não encontrada"):
        await repo.get_tool_by_id("x")


async def test_tool_repo_ordem_do_backend_e_uma_tool_por_id_a_primeira(backend):
    """A ordem é a do backend (não a dos ids pedidos) e o id repetido fica com a primeira."""
    repo = backend.tool_repo([_tool("b"), _tool("dup", name="primeira"), _tool("a"), _tool("dup", name="segunda")])

    by_ids = await repo.get_tools_by_ids(["a", "dup", "b"])
    all_active = await repo.get_all_active_tools()

    expected = [("b", "b"), ("dup", "primeira"), ("a", "a")]
    assert [(t.id, t.name) for t in by_ids] == expected
    assert [(t.id, t.name) for t in all_active] == expected
    assert (await repo.get_tool_by_id("dup")).name == "primeira"


async def test_tool_repo_devolve_a_entidade_completa_igual_a_gravada(backend):
    tool = Tool(
        id="clima",
        name="Clima",
        description="d",
        route="https://api.example/v1/{cidade}",
        http_method=HttpMethod.POST,
        parameters=[
            ToolParameter(name="cidade", type=ParameterType.STRING, description="c", required=True),
            ToolParameter(name="dias", type=ParameterType.INTEGER, description="n", default_value=3),
        ],
        instructions="use",
        headers={"Accept": "application/json"},
    )
    repo = backend.tool_repo([tool])

    assert await repo.get_tools_by_ids(["clima"]) == [tool]
    assert await repo.get_tool_by_id("clima") == tool
