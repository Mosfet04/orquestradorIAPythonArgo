"""FA-04: build -> persist -> swap no caso de uso de config, com o runtime falso e os repositórios em memória.

A troca no runtime real (rotas do AgentOS, AG-UI, run em stream) está em ``test_agno_runtime_swap.py`` e
``tests/integration/test_fa04_hot_swap.py``.
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from pathlib import Path

import pytest

from src.application.use_cases.config_admin_use_case import ConfigAdmin, ConfigWrite
from src.application.use_cases.config_change import ConfigChange, ConfigNotBuildable
from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase
from src.application.use_cases.get_active_teams_use_case import GetActiveTeamsUseCase
from src.domain.entities.agent_config import AgentConfig
from src.domain.ports import InvalidModelConfigError
from src.domain.repositories.versioning import ConfigStoreReadOnly, Versioned
from src.infrastructure.dependency_injection import build_config_admin
from tests.fakes.admin_config_app import agent, agent_body, fake_audit_log, fake_runtime_swap, route_guard, team, tool
from tests.fakes.config_stores import YamlBackend, agent_document
from tests.fakes.logger import RecordingLogger
from tests.fakes.repositories import (
    InMemoryAgentConfigRepository,
    InMemoryTeamConfigRepository,
    InMemoryToolRepository,
)
from tests.fakes.runtime import FakeAgentRuntime

TEAM_BODY = {"id": "t1", "nome": "T", "factory_ia_model": "ollama", "model": "qwen3", "member_ids": ["a1"]}


class _YieldingAgents(InMemoryAgentConfigRepository):
    """Cede o loop em cada leitura e gravação, como um driver real: abre espaço para intercalar escritas."""

    async def get_active_agents(self) -> list[AgentConfig]:
        await asyncio.sleep(0)
        return await super().get_active_agents()

    async def list_all(self) -> list[Versioned[AgentConfig]]:
        await asyncio.sleep(0)
        return await super().list_all()

    async def save(self, entity: AgentConfig, expected_version: int) -> int:
        await asyncio.sleep(0)
        return await super().save(entity, expected_version)


class _SlowFirstRead(InMemoryAgentConfigRepository):
    """A primeira leitura das ativas só devolve o que leu depois de ``release`` (montagem demorada)."""

    def __init__(self, items: list[AgentConfig]) -> None:
        super().__init__(items)
        self.reading = asyncio.Event()
        self.release = asyncio.Event()

    async def get_active_agents(self) -> list[AgentConfig]:
        active = await super().get_active_agents()
        if not self.reading.is_set():
            self.reading.set()
            await self.release.wait()
        return active


class _Admin:
    def __init__(
        self,
        agents: InMemoryAgentConfigRepository | None = None,
        teams: InMemoryTeamConfigRepository | None = None,
        tools: InMemoryToolRepository | None = None,
        runtime: FakeAgentRuntime | None = None,
    ) -> None:
        self.agents = agents or InMemoryAgentConfigRepository()
        self.teams = teams or InMemoryTeamConfigRepository()
        self.tools = tools or InMemoryToolRepository()
        self.runtime = runtime or FakeAgentRuntime()
        self.logger = RecordingLogger()
        self.admin: ConfigAdmin = build_config_admin(
            self.agents,
            self.teams,
            self.tools,
            self.logger,
            runtime=fake_runtime_swap(self.agents, self.teams, self.runtime),
            audit=fake_audit_log(),
            route_guard=route_guard(),
        )

    def served(self) -> tuple[list[tuple[str, str]], list[str]]:
        agents, teams = self.runtime.served
        return [(a.id, a.nome) for a in agents], [t.id for t in teams]  # type: ignore[attr-defined]


# ── ConfigChange ────────────────────────────────────────────────────


def test_mudanca_substitui_no_lugar_acrescenta_id_novo_e_tira_o_inativo_ou_removido():
    active = [agent("a1"), agent("a2"), agent("a3")]
    changed = agent("a2")
    changed.nome = "Novo nome"

    assert [c.nome for c in ConfigChange("a2", changed).applied(active)] == ["Agente a1", "Novo nome", "Agente a3"]
    assert [c.id for c in ConfigChange("a4", agent("a4")).applied(active)] == ["a1", "a2", "a3", "a4"]
    assert [c.id for c in ConfigChange("a2", agent("a2", active=False)).applied(active)] == ["a1", "a3"]
    assert [c.id for c in ConfigChange("a2").applied(active)] == ["a1", "a3"]


# ── casos de uso com a mudança ──────────────────────────────────────


async def test_agente_alterado_que_nao_monta_recusa_com_o_tipo_do_erro_depois_do_log():
    logger = RecordingLogger()
    runtime = FakeAgentRuntime(failures={"a2": InvalidModelConfigError("texto do provider")})
    use_case = GetActiveAgentsUseCase(runtime, InMemoryAgentConfigRepository([agent("a1")]), logger)

    with pytest.raises(ConfigNotBuildable) as caught:
        await use_case.execute(ConfigChange("a2", agent("a2")))

    assert caught.value.error_type == "InvalidModelConfigError"
    assert str(caught.value) == "Configuração não monta no runtime"
    assert [r.context["agent_id"] for r in logger.records if r.level == "error"] == ["a2"]


async def test_outro_agente_que_ja_nao_montava_nao_recusa_a_mudanca():
    runtime = FakeAgentRuntime(failures={"quebrado": RuntimeError("x")})
    use_case = GetActiveAgentsUseCase(
        runtime, InMemoryAgentConfigRepository([agent("quebrado"), agent("a1")]), RecordingLogger()
    )

    agents = await use_case.execute(ConfigChange("a1", agent("a1")))

    assert [a.id for a in agents] == ["a1"]


async def test_team_alterado_sem_membro_montado_recusa():
    runtime = FakeAgentRuntime()
    agents = [await runtime.build_agent(agent("a1"))]
    use_case = GetActiveTeamsUseCase(runtime, InMemoryTeamConfigRepository(), RecordingLogger())

    with pytest.raises(ConfigNotBuildable) as caught:
        await use_case.execute(agents, ConfigChange("t1", team("t1", ["fora-do-runtime"])))

    assert caught.value.error_type == "ValueError"


# ── build -> persist -> swap nas escritas ───────────────────────────


async def test_alteracao_monta_grava_e_troca_e_devolve_a_revisao():
    world = _Admin(InMemoryAgentConfigRepository([agent("a1"), agent("a2")]))

    written = await world.admin.agents.replace("a1", agent_body("a1", nome="Renomeado"), 0, actor="admin")

    assert isinstance(written, ConfigWrite)
    assert (written.item.version, written.runtime_revision) == (1, 1)
    assert world.served() == ([("a1", "Renomeado"), ("a2", "Agente a2")], [])
    assert [i.entity.nome for i in await world.agents.list_all()] == ["Renomeado", "Agente a2"]


async def test_config_que_nao_monta_nao_grava_nem_troca_e_loga_a_recusa():
    runtime = FakeAgentRuntime(failures={"a2": InvalidModelConfigError("x")})
    world = _Admin(InMemoryAgentConfigRepository([agent("a1")]), runtime=runtime)
    world.runtime.swap([await runtime.build_agent(agent("a1"))], [])

    with pytest.raises(ConfigNotBuildable):
        await world.admin.agents.create(agent_body("a2"), actor="admin")

    assert [i.entity.id for i in await world.agents.list_all()] == ["a1"]
    assert (world.runtime.revision, world.served()) == (1, ([("a1", "Agente a1")], []))
    assert [(r.level, r.context) for r in world.logger.records] == [
        ("warning", {"kind": "agents", "refusal": "ConfigNotBuildable", "field": None})
    ]


async def test_store_so_leitura_recusa_toda_escrita_antes_de_montar(tmp_path: Path):
    """A montagem tem efeito colateral (o RAG repara o índice e insere na coleção): YAML recusa antes dela."""
    backend = YamlBackend(tmp_path)
    agents = backend.agents([agent_document(agent("a1"))])
    teams = backend.teams([])
    tools = backend.tools([])
    runtime = FakeAgentRuntime(failures={"a2": InvalidModelConfigError("x")})
    logger = RecordingLogger()
    admin = build_config_admin(
        agents, teams, tools, logger, runtime=fake_runtime_swap(agents, teams, runtime), audit=None,
        route_guard=route_guard(),
    )

    writes: list[Callable[[], Awaitable[object]]] = [
        lambda: admin.agents.replace("a1", agent_body("a1", nome="Renomeado"), 0, actor="admin"),  # monta
        lambda: admin.agents.create(agent_body("a2"), actor="admin"),  # não monta
        lambda: admin.teams.create(TEAM_BODY, actor="admin"),
        lambda: admin.tools.create({"id": "t"}, actor="admin"),
        lambda: admin.agents.deactivate("a1", 0, actor="admin"),
        lambda: admin.agents.remove("a1", 0, actor="admin"),
    ]
    for write in writes:
        with pytest.raises(ConfigStoreReadOnly):
            await write()

    assert (runtime.agent_builds, runtime.team_builds, runtime.revision) == ([], [], 0)
    assert {r.context["refusal"] for r in logger.records} == {"ConfigStoreReadOnly"}


async def test_criacao_de_team_e_remocoes_trocam_o_conjunto_servido():
    world = _Admin(InMemoryAgentConfigRepository([agent("a1"), agent("a2")]))

    created = await world.admin.teams.create(TEAM_BODY, actor="admin")
    deactivated = await world.admin.agents.deactivate("a2", 0, actor="admin")
    removed_revision = await world.admin.teams.remove("t1", created.item.version, actor="admin")

    assert (created.runtime_revision, deactivated.runtime_revision, removed_revision) == (1, 2, 3)
    assert world.served() == ([("a1", "Agente a1")], [])


async def test_escrita_de_tool_grava_antes_de_montar_porque_o_agente_le_as_tools_do_store():
    tools = InMemoryToolRepository([tool("cep")])
    world = _Admin(InMemoryAgentConfigRepository([agent("a1", tools_ids=["cep"])]), tools=tools)
    shown = (await world.admin.tools.get("cep")).entity
    descriptions_at_build: list[str] = []
    build_agent = world.runtime.build_agent

    async def build_reading_tools(config: AgentConfig) -> object:
        descriptions_at_build.extend(t.description for t in await tools.get_tools_by_ids(config.tools_ids or []))
        return await build_agent(config)

    world.runtime.build_agent = build_reading_tools  # type: ignore[method-assign]

    written = await world.admin.tools.replace("cep", {**shown, "description": "nova"}, 0, actor="admin")

    assert written.runtime_revision == 1
    assert descriptions_at_build == ["nova"]


async def test_tool_gravada_com_falha_na_troca_loga_e_sobe_sem_texto_da_excecao():
    class _BrokenActive(InMemoryAgentConfigRepository):
        async def get_active_agents(self) -> list[AgentConfig]:
            raise RuntimeError("segredo-do-driver")

    world = _Admin(_BrokenActive(), tools=InMemoryToolRepository([tool("cep")]))
    shown = (await world.admin.tools.get("cep")).entity

    with pytest.raises(RuntimeError):
        await world.admin.tools.replace("cep", {**shown, "description": "nova"}, 0, actor="admin")

    assert [i.entity.description for i in await world.tools.list_all()] == ["nova"]
    assert [(r.level, r.message, r.context) for r in world.logger.records] == [
        (
            "error",
            "Config gravada, mas o runtime não foi trocado; use POST /admin/config/reload",
            {"kind": "tools", "error_type": "RuntimeError"},
        )
    ]


async def test_reload_relê_o_store_e_troca():
    agents = InMemoryAgentConfigRepository([agent("a1")])
    world = _Admin(agents)
    await agents.save(agent("a2"), 0)  # gravado por fora da API (ex.: outro processo, arquivo YAML)

    assert await world.admin.reload() == 1
    assert world.served() == ([("a1", "Agente a1"), ("a2", "Agente a2")], [])


async def test_reload_que_falha_loga_so_o_tipo_do_erro_e_nao_troca():
    class _Broken(InMemoryAgentConfigRepository):
        async def get_active_agents(self) -> list[AgentConfig]:
            raise RuntimeError("segredo-do-driver")

    runtime, logger = FakeAgentRuntime(), RecordingLogger()
    swap = fake_runtime_swap(_Broken(), InMemoryTeamConfigRepository(), runtime, logger)

    with pytest.raises(RuntimeError):
        await swap.reload()

    assert runtime.revision == 0
    assert [(r.level, r.message, r.context) for r in logger.records] == [
        ("error", "Falha ao recarregar a config; o runtime anterior segue servindo", {"error_type": "RuntimeError"})
    ]


async def test_duas_escritas_simultaneas_sao_serializadas_e_a_ultima_troca_tem_as_duas():
    """Sem a trava, a montagem de uma não veria a gravação da outra e a última troca perderia uma mudança."""
    world = _Admin(_YieldingAgents([agent("a1"), agent("a2")]))

    first, second = await asyncio.gather(
        world.admin.agents.replace("a1", agent_body("a1", nome="Um"), 0, actor="admin"),
        world.admin.agents.replace("a2", agent_body("a2", nome="Dois"), 0, actor="admin"),
    )

    assert sorted((first.runtime_revision, second.runtime_revision)) == [1, 2]
    assert world.served() == ([("a1", "Um"), ("a2", "Dois")], [])


async def test_reload_concorrente_com_escrita_nao_troca_por_um_conjunto_velho():
    """O reload lê a config e demora a trocar; sem a trava, a escrita trocaria antes e o reload a desfaria."""
    agents = _SlowFirstRead([agent("a1")])
    world = _Admin(agents)
    reload = asyncio.create_task(world.admin.reload())
    await agents.reading.wait()
    write = asyncio.create_task(world.admin.agents.replace("a1", agent_body("a1", nome="Novo"), 0, actor="admin"))
    for _ in range(20):
        await asyncio.sleep(0)
    agents.release.set()

    assert (await reload, (await write).runtime_revision) == (1, 2)
    assert world.served() == ([("a1", "Novo")], [])
