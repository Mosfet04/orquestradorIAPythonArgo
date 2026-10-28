"""QA FA-01: lacunas do contrato de escrita das portas de config.

Complementa ``test_config_write_contract.py`` (sem repetir): recriação após ``delete``, ``active`` e
a separação ``list_all`` x ``get_active_*``, versão inválida, concorrência com mais de dois
escritores, ids repetidos, ordem estável após ``save``, isolamento entre os três tipos com o mesmo
id e ida-e-volta de entidades completas (rag, parâmetros, headers). Roda nos backends graváveis
(memória, Mongo com coleção fake e, ``live``, Mongo real).
"""

from __future__ import annotations

import asyncio

import pytest

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.rag_config import RagConfig, SearchStrategy
from src.domain.entities.team_config import TeamConfig
from src.domain.entities.tool import HttpMethod, ParameterType, Tool, ToolParameter
from src.domain.repositories.versioning import ConfigVersionConflict, Versioned
from tests.fakes.config_stores import CONFIG_KINDS, make_agent, make_team, make_tool


async def test_delete_seguido_de_save_na_versao_0_recria_o_id(writable, kind):
    repo = kind.repo(writable, [kind.make("a")])
    assert await repo.save(kind.make("a", "v1"), 0) == 1
    await repo.delete("a", 1)

    assert await repo.list_all() == []
    assert await repo.save(kind.make("a", "recriada"), 0) == 1

    assert await repo.list_all() == [Versioned(kind.make("a", "recriada"), 1)]
    with pytest.raises(ConfigVersionConflict):  # o delete já feito não vale de novo
        await repo.delete("a", 0)


async def test_save_que_desativa_some_do_runtime_mas_continua_na_listagem_completa(writable, kind):
    repo = kind.repo(writable, [kind.make("a"), kind.make("b")])

    await repo.save(kind.make("a", "off", active=False), 0)

    assert [e.id for e in await kind.active(repo)] == ["b"]
    listed = [(v.entity.id, v.entity.active, v.version) for v in await repo.list_all()]
    assert listed == [("a", False, 1), ("b", True, 0)]
    # a busca por id do runtime não filtra por active (comportamento anterior do contrato)
    assert (await kind.get(repo, "a")).active is False

    await repo.save(kind.make("a", "on"), 1)
    assert [e.id for e in await kind.active(repo)] == ["a", "b"]


async def test_save_mantem_a_posicao_na_ordem_estavel(writable, kind):
    repo = kind.repo(writable, [kind.make("a"), kind.make("b"), kind.make("c")])

    await repo.save(kind.make("b", "mudou"), 0)
    await repo.save(kind.make("a", "mudou"), 0)

    assert [(v.entity.id, v.version) for v in await repo.list_all()] == [("a", 1), ("b", 1), ("c", 0)]


@pytest.mark.parametrize("bad", [-1, -5])
async def test_versao_negativa_conflita_sem_gravar_nem_apagar(writable, kind, bad):
    repo = kind.repo(writable, [kind.make("a")])

    with pytest.raises(ConfigVersionConflict):
        await repo.save(kind.make("a", "x"), bad)
    with pytest.raises(ConfigVersionConflict):
        await repo.save(kind.make("novo"), bad)
    with pytest.raises(ConfigVersionConflict):
        await repo.delete("a", bad)

    assert await repo.list_all() == [Versioned(kind.make("a"), 0)]


@pytest.mark.parametrize("bad", [True, False, 1.0, "0", None])
async def test_versao_que_nao_e_inteiro_e_recusada_antes_do_store(writable, kind, bad):
    """``True`` (que é ``int`` em Python) e ``1.0`` casariam a versão 1 por igualdade: recusados."""
    repo = kind.repo(writable, [kind.make("a")])
    await repo.save(kind.make("a", "v1"), 0)

    with pytest.raises(TypeError):
        await repo.save(kind.make("a", "x"), bad)
    with pytest.raises(TypeError):
        await repo.delete("a", bad)

    assert await repo.list_all() == [Versioned(kind.make("a", "v1"), 1)]


async def test_muitos_saves_concorrentes_na_mesma_versao_so_um_vence(writable, kind):
    repo = kind.repo(writable, [kind.make("a")])
    await repo.save(kind.make("a", "base"), 0)

    results = await asyncio.gather(
        *(repo.save(kind.make("a", f"w{i}"), 1) for i in range(8)), return_exceptions=True
    )

    assert sorted(r for r in results if not isinstance(r, Exception)) == [2]
    assert sum(isinstance(r, ConfigVersionConflict) for r in results) == 7
    (only,) = await repo.list_all()
    assert only.version == 2
    assert kind.label(only.entity) in {f"w{i}" for i in range(8)}


async def test_save_e_delete_concorrentes_na_mesma_versao_um_vence(writable, kind):
    repo = kind.repo(writable, [kind.make("a")])

    results = await asyncio.gather(
        repo.save(kind.make("a", "x"), 0), repo.delete("a", 0), return_exceptions=True
    )

    assert sum(isinstance(r, ConfigVersionConflict) for r in results) == 1
    listed = await repo.list_all()
    assert listed in ([Versioned(kind.make("a", "x"), 1)], [])
    # quem venceu é coerente com o estado: save venceu <=> o documento existe
    assert (results[0] == 1) == bool(listed)


async def test_id_repetido_save_substitui_exatamente_um_dos_documentos(writable, kind):
    """A porta não promete qual dos repetidos é afetado (o Mongo real escolhe); promete um só e a contagem."""
    repo = kind.repo(writable, [kind.make("a", "p"), kind.make("b"), kind.make("a", "s")])

    await repo.save(kind.make("a", "novo"), 0)

    saved = await repo.list_all()
    assert len(saved) == 3
    (replaced,) = [v for v in saved if v.version == 1]
    (untouched,) = [v for v in saved if v.version == 0 and v.entity.id == "a"]
    assert (replaced.entity.id, kind.label(replaced.entity)) == ("a", "novo")
    assert kind.label(untouched.entity) in {"p", "s"}


async def test_id_repetido_delete_remove_exatamente_um_dos_documentos(writable, kind):
    repo = kind.repo(writable, [kind.make("a", "p"), kind.make("b"), kind.make("a", "s")])

    await repo.delete("a", 0)

    remaining = await repo.list_all()
    assert sorted(v.entity.id for v in remaining) == ["a", "b"]
    assert [kind.label(v.entity) for v in remaining if v.entity.id == "a"] in (["p"], ["s"])


async def test_save_nao_altera_a_entidade_recebida(writable, kind):
    repo = kind.repo(writable, [])
    entity = kind.make("a", "orig")

    await repo.save(entity, 0)

    assert entity == kind.make("a", "orig")


async def test_save_nao_guarda_referencia_a_entidade_recebida(writable, kind):
    repo = kind.repo(writable, [])
    entity = kind.make("a", "orig")
    await repo.save(entity, 0)

    entity.active = False  # mutação posterior do chamador não chega ao store

    assert await repo.list_all() == [Versioned(kind.make("a", "orig"), 1)]


async def test_tres_tipos_com_o_mesmo_id_sao_independentes(writable):
    agents = writable.agent_repo([make_agent("x")])
    teams = writable.team_repo([make_team("x")])
    tools = writable.tool_repo([make_tool("x")])

    assert await agents.save(make_agent("x", "a1"), 0) == 1
    await teams.delete("x", 0)
    assert await tools.save(make_tool("x", "t1"), 0) == 1
    assert await tools.save(make_tool("x", "t2"), 1) == 2

    assert await agents.list_all() == [Versioned(make_agent("x", "a1"), 1)]
    assert await teams.list_all() == []
    assert await tools.list_all() == [Versioned(make_tool("x", "t2"), 2)]


# ── ida e volta de entidades completas ──────────────────────────────


def _rich_agent() -> AgentConfig:
    return AgentConfig(
        id="rich",
        nome="Rico",
        factory_ia_model="openai",
        model="gpt",
        descricao=None,
        prompt="p",
        tools_ids=["t1", "t2"],
        rag_config=RagConfig(
            active=True,
            doc_name="doc",
            model="emb",
            factory_ia_model="ollama",
            search_strategy=SearchStrategy.HIERARCHICAL,
            model_params={"dimensions": 8, "normalize": True},
            base_url="https://emb.example.com/v1",
            api_key_ref="env:OLLAMA_API_KEY",
        ),
        user_memory_active=True,
        summary_active=True,
        active=False,
        model_params={"temperature": 0.2, "max_tokens": 10, "stream": False, "stop": "x"},
        base_url="https://llm.example.com/v1",
        api_key_ref="env:OPENAI_API_KEY",
    )


def _rich_team() -> TeamConfig:
    return TeamConfig(
        id="rich",
        nome="Time",
        factory_ia_model="openai",
        model="gpt",
        member_ids=["a", "b"],
        mode="coordinate",
        descricao="d",
        prompt="p",
        user_memory_active=False,
        summary_active=True,
        active=False,
        model_params={"temperature": 0.0},
        base_url="https://llm.example.com/v1",
        api_key_ref="file:/run/secrets/openai",
    )


def _rich_tool() -> Tool:
    return Tool(
        id="rich",
        name="Rica",
        description="d",
        route="https://api.example.com/x",
        http_method=HttpMethod.PATCH,
        parameters=[
            ToolParameter(name="q", type=ParameterType.STRING, description="busca", required=True),
            ToolParameter(name="n", type=ParameterType.INTEGER, description="n", default_value=3),
            ToolParameter(name="o", type=ParameterType.OBJECT, description="o", default_value={"a": [1, 2]}),
        ],
        instructions="use com cuidado",
        headers={"X-Trace": "1"},
        active=False,
    )


@pytest.mark.parametrize(
    ("kind_name", "rich"),
    [("agent", _rich_agent), ("team", _rich_team), ("tool", _rich_tool)],
)
async def test_entidade_completa_gravada_e_relida_pelo_runtime_e_pela_listagem_e_igual(writable, kind_name, rich):
    kind = CONFIG_KINDS[kind_name]
    repo = kind.repo(writable, [])

    assert await repo.save(rich(), 0) == 1

    assert await kind.get(repo, "rich") == rich()
    assert await repo.list_all() == [Versioned(rich(), 1)]


@pytest.mark.parametrize(
    ("kind_name", "minimal"),
    [
        (
            "agent",
            lambda: AgentConfig(
                id="m", nome="n", factory_ia_model="ollama", model="m", descricao=None, prompt=""
            ),
        ),
        ("team", lambda: TeamConfig(id="m", nome="n", factory_ia_model="ollama", model="m", member_ids=["a"])),
        (
            "tool",
            lambda: Tool(
                id="m", name="n", description="d", route="https://x.invalid", http_method=HttpMethod.GET,
                parameters=[], instructions=None, headers=None,
            ),
        ),
    ],
)
async def test_entidade_com_opcionais_nulos_volta_igual_sem_virar_default(writable, kind_name, minimal):
    kind = CONFIG_KINDS[kind_name]
    repo = kind.repo(writable, [])

    await repo.save(minimal(), 0)

    assert await kind.get(repo, "m") == minimal()
