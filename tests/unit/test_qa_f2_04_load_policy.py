"""QA F2-04: política de carga dos casos de uso e cache do controller, com handles do runtime falso.

Complementa ``test_get_active_*_use_case.py`` (dev): ordem do resultado quando a conclusão é
invertida, cancelamento externo (não é falha de entidade), team com os agentes recebidos, e o
controller real (cache com TTL, hit/miss, fallback expirado) sobre casos de uso reais.
"""

from __future__ import annotations

import asyncio
import threading
from collections.abc import Sequence
from datetime import timedelta
from typing import Any

import pytest

from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase
from src.application.use_cases.get_active_teams_use_case import GetActiveTeamsUseCase
from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.team_config import TeamConfig
from src.domain.ports import AgentHandle
from src.presentation.controllers.orquestrador_controller import OrquestradorController
from tests.fakes import InMemoryAgentConfigRepository, InMemoryTeamConfigRepository, RecordingLogger
from tests.fakes.runtime import FakeAgentHandle, FakeAgentRuntime, FakeTeamHandle


def _agent(agent_id: str) -> AgentConfig:
    return AgentConfig(
        id=agent_id, nome=f"A {agent_id}", factory_ia_model="ollama", model="m", descricao="d", prompt="p"
    )


def _team(team_id: str, members: list[str]) -> TeamConfig:
    return TeamConfig(
        id=team_id, nome=f"T {team_id}", factory_ia_model="ollama", model="m", member_ids=members, mode="route"
    )


class _ListAgents:
    def __init__(self, configs: list[AgentConfig]) -> None:
        self.configs = configs
        self.fail = False

    async def get_active_agents(self) -> list[AgentConfig]:
        if self.fail:
            raise ConnectionError("mongo caiu")
        return list(self.configs)


# ── agentes ──────────────────────────────────────────────────────────────────────────────────────


async def test_resultado_segue_a_ordem_das_configs_mesmo_com_conclusao_invertida_e_falha_no_meio() -> None:
    finished: list[str] = []
    gates = {"a1": asyncio.Event(), "a2": asyncio.Event(), "a3": asyncio.Event()}

    class _Reversed(FakeAgentRuntime):
        async def build_agent(self, config: AgentConfig) -> FakeAgentHandle:
            if config.id == "a3":  # o último a começar termina primeiro e libera os outros, de trás pra frente
                gates["a2"].set()
            else:
                await asyncio.wait_for(gates[config.id].wait(), timeout=2)
            finished.append(config.id)
            if config.id == "a2":
                gates["a1"].set()
                raise RuntimeError("falha do meio")
            return await super().build_agent(config)

    repository = InMemoryAgentConfigRepository([_agent("a1"), _agent("a2"), _agent("a3")])
    logger = RecordingLogger()

    agents = await GetActiveAgentsUseCase(_Reversed(), repository, logger).execute()

    assert finished == ["a3", "a2", "a1"]
    assert [a.id for a in agents] == ["a1", "a3"]
    assert [r.context["agent_id"] for r in logger.records if r.level == "error"] == ["a2"]


async def test_cancelar_a_carga_cancela_as_montagens_pendentes_e_nao_loga_falha_de_agente() -> None:
    started = asyncio.Event()
    cancelled: list[str] = []

    class _Hung(FakeAgentRuntime):
        async def build_agent(self, config: AgentConfig) -> FakeAgentHandle:
            started.set()
            try:
                await asyncio.sleep(30)
            except asyncio.CancelledError:
                cancelled.append(config.id)
                raise
            raise AssertionError("não deveria terminar")  # pragma: no cover

    logger = RecordingLogger()
    use_case = GetActiveAgentsUseCase(
        _Hung(), InMemoryAgentConfigRepository([_agent("a1"), _agent("a2")]), logger
    )
    task = asyncio.create_task(use_case.execute())
    await asyncio.wait_for(started.wait(), timeout=2)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert sorted(cancelled) == ["a1", "a2"]
    assert [r for r in logger.records if r.level == "error"] == []


async def test_repositorio_que_falha_sobe_sem_montar_nada() -> None:
    runtime = FakeAgentRuntime()
    repository = _ListAgents([_agent("a1")])
    repository.fail = True

    with pytest.raises(ConnectionError):
        await GetActiveAgentsUseCase(runtime, repository, RecordingLogger()).execute()  # type: ignore[arg-type]

    assert runtime.agent_builds == []


# ── teams ────────────────────────────────────────────────────────────────────────────────────────


async def test_team_recebe_exatamente_os_agentes_dados_na_ordem_e_membros_ausentes_nao_viram_membro() -> None:
    runtime = FakeAgentRuntime()
    given: Sequence[AgentHandle] = [FakeAgentHandle("b", "B"), FakeAgentHandle("a", "A")]
    use_case = GetActiveTeamsUseCase(
        runtime, InMemoryTeamConfigRepository([_team("t1", ["a", "fantasma"])]), RecordingLogger()
    )

    teams = await use_case.execute(given)

    assert teams == [FakeTeamHandle(id="t1", nome="T t1", member_ids=("a",))]
    [build] = runtime.team_builds
    assert build.agent_ids == ("b", "a") and build.on_event_loop is False


async def test_cancelar_a_carga_de_teams_nao_vira_falha_de_team() -> None:
    entered = threading.Event()
    release = threading.Event()

    class _Blocking(FakeAgentRuntime):
        def build_team(self, config: TeamConfig, agents: Sequence[AgentHandle]) -> FakeTeamHandle:
            entered.set()
            release.wait(timeout=5)
            return super().build_team(config, agents)

    logger = RecordingLogger()
    use_case = GetActiveTeamsUseCase(_Blocking(), InMemoryTeamConfigRepository([_team("t1", ["a"])]), logger)
    task = asyncio.create_task(use_case.execute([FakeAgentHandle("a", "A")]))
    assert await asyncio.get_running_loop().run_in_executor(None, entered.wait, 2)

    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
    finally:
        release.set()

    assert [r for r in logger.records if r.level == "error"] == []


async def test_event_loop_segue_livre_enquanto_o_team_monta() -> None:
    entered = threading.Event()
    release = threading.Event()

    class _Blocking(FakeAgentRuntime):
        def build_team(self, config: TeamConfig, agents: Sequence[AgentHandle]) -> FakeTeamHandle:
            entered.set()
            assert release.wait(timeout=5)
            return super().build_team(config, agents)

    use_case = GetActiveTeamsUseCase(_Blocking(), InMemoryTeamConfigRepository([_team("t1", ["a"])]), RecordingLogger())
    task = asyncio.create_task(use_case.execute([FakeAgentHandle("a", "A")]))
    assert await asyncio.get_running_loop().run_in_executor(None, entered.wait, 2)

    # O loop atendeu o run_in_executor acima com o build_team ainda bloqueado na thread.
    assert not task.done()
    release.set()

    assert [t.id for t in await task] == ["t1"]


# ── controller real com casos de uso reais ───────────────────────────────────────────────────────


class _Cache:
    """Guarda os hits e misses que o controller entrega aos callbacks de métrica."""

    def __init__(self) -> None:
        self.hits: list[str] = []
        self.misses: list[str] = []


class _Env:
    def __init__(self) -> None:
        self.cache = _Cache()
        self.logger = RecordingLogger()
        self.runtime = FakeAgentRuntime()
        self.agents_repo = _ListAgents([_agent("a1"), _agent("a2")])
        self.teams_repo = InMemoryTeamConfigRepository([_team("t1", ["a1", "a2"])])
        self.controller = OrquestradorController(
            get_active_agents_use_case=GetActiveAgentsUseCase(
                self.runtime, self.agents_repo, self.logger  # type: ignore[arg-type]
            ),
            get_active_teams_use_case=GetActiveTeamsUseCase(self.runtime, self.teams_repo, self.logger),
            logger=self.logger,
            on_cache_hit=self.cache.hits.append,
            on_cache_miss=self.cache.misses.append,
        )

    def expire(self, which: str) -> None:
        entry: Any = self.controller._cache if which == "agents" else self.controller._team_cache
        entry.created_at -= entry.ttl + timedelta(seconds=1)


@pytest.fixture
def env() -> _Env:
    return _Env()


async def test_cache_de_agentes_devolve_os_mesmos_handles_no_hit_e_conta_hit_e_miss(env: _Env) -> None:
    first = await env.controller.get_agents()
    second = await env.controller.get_agents()

    assert [a.id for a in first] == ["a1", "a2"]
    assert second is first and all(x is y for x, y in zip(first, second, strict=True))
    assert (env.cache.misses, env.cache.hits) == (["agents"], ["agents"])
    assert len(env.runtime.agent_builds) == 2  # montou uma vez só
    assert env.controller.get_cache_stats()["agents"]["hit_count"] == 1


async def test_get_teams_monta_agentes_antes_e_o_team_recebe_os_handles_do_cache(env: _Env) -> None:
    teams = await env.controller.get_teams()
    agents = await env.controller.get_agents()

    [build] = env.runtime.team_builds
    assert build.agent_ids == ("a1", "a2") == tuple(a.id for a in agents)
    assert teams == [FakeTeamHandle(id="t1", nome="T t1", member_ids=("a1", "a2"))]
    assert (await env.controller.get_teams()) is teams
    assert env.cache.hits == ["agents", "teams"] and env.cache.misses == ["teams", "agents"]


async def test_cache_expirado_recarrega_do_repositorio_com_a_config_nova(env: _Env) -> None:
    await env.controller.get_agents()
    env.agents_repo.configs = [_agent("a1"), _agent("a3")]
    env.expire("agents")

    agents = await env.controller.get_agents()

    assert [a.id for a in agents] == ["a1", "a3"]
    assert env.cache.misses == ["agents", "agents"] and env.cache.hits == []
    assert env.controller.get_cache_stats()["agents"]["is_expired"] is False


async def test_cache_expirado_com_repositorio_fora_serve_o_expirado_com_aviso_e_nao_levanta(env: _Env) -> None:
    first = await env.controller.get_agents()
    env.expire("agents")
    env.agents_repo.fail = True

    again = await env.controller.get_agents()

    assert again is first
    assert "Usando cache expirado como fallback" in env.logger.messages("warning")


async def test_sem_cache_e_repositorio_fora_a_falha_de_agentes_sobe(env: _Env) -> None:
    env.agents_repo.fail = True

    with pytest.raises(ConnectionError):
        await env.controller.get_agents()


async def test_falha_ao_carregar_teams_cai_no_cache_expirado_ou_em_lista_vazia(env: _Env) -> None:
    class _BrokenTeams(InMemoryTeamConfigRepository):
        broken = False

        async def get_active_teams(self) -> list[TeamConfig]:
            if self.broken:
                raise ConnectionError("mongo caiu")
            return await super().get_active_teams()

    repo = _BrokenTeams([_team("t1", ["a1"])])
    env.controller._teams_use_case = GetActiveTeamsUseCase(env.runtime, repo, env.logger)
    repo.broken = True
    assert await env.controller.get_teams() == []  # sem cache: vazio, startup segue

    repo.broken = False
    teams = await env.controller.get_teams()
    env.expire("teams")
    repo.broken = True

    assert await env.controller.get_teams() is teams  # com cache expirado: serve o expirado


async def test_refresh_recria_agentes_e_teams_com_handles_novos_e_zera_o_hit_count(env: _Env) -> None:
    old_agents = await env.controller.get_agents()
    await env.controller.get_teams()
    await env.controller.get_agents()

    await env.controller.refresh_agents()

    new_agents = await env.controller.get_agents()
    assert new_agents is not old_agents and [a.id for a in new_agents] == ["a1", "a2"]
    stats = env.controller.get_cache_stats()
    assert stats["agents"]["agent_count"] == 2 and stats["teams"]["team_count"] == 1
    assert stats["agents"]["hit_count"] == 1  # só o get_agents depois do refresh
    assert "Cache de agentes e teams atualizado" in env.logger.messages("info")


async def test_chamadas_concorrentes_com_cache_frio_nao_corrompem_e_ficam_consistentes(env: _Env) -> None:
    results = await asyncio.gather(*(env.controller.get_agents() for _ in range(5)))

    assert all([a.id for a in r] == ["a1", "a2"] for r in results)
    stats = env.controller.get_cache_stats()["agents"]
    assert stats["agent_count"] == 2 and stats["status"] == "active"
