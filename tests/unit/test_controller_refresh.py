"""BUG-F2-06-QA-1: ``refresh_agents`` só troca o cache depois de recarregar agentes **e** teams com sucesso.

Antes, o cache era zerado antes da recarga: com o repositório falhando (Mongo fora, YAML quebrado) o
``/metrics/cache`` ficava vazio, ao contrário do que o README promete ("no refresh, fica o cache
anterior"). Casos de uso reais com runtime e repositórios falsos.
"""

from __future__ import annotations

import pytest

from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase
from src.application.use_cases.get_active_teams_use_case import GetActiveTeamsUseCase
from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.team_config import TeamConfig
from src.presentation.controllers.orquestrador_controller import OrquestradorController
from tests.fakes import InMemoryAgentConfigRepository, InMemoryTeamConfigRepository, RecordingLogger
from tests.fakes.runtime import FakeAgentRuntime


class _Breakable:
    """Mixin: depois de ``broken = True``, toda leitura levanta (como um banco fora do ar)."""

    broken = False

    def _check(self) -> None:
        if self.broken:
            raise RuntimeError("repositório fora do ar")


class _Agents(_Breakable, InMemoryAgentConfigRepository):
    async def get_active_agents(self) -> list[AgentConfig]:
        self._check()
        return await super().get_active_agents()


class _Teams(_Breakable, InMemoryTeamConfigRepository):
    async def get_active_teams(self) -> list[TeamConfig]:
        self._check()
        return await super().get_active_teams()


def _agent(agent_id: str) -> AgentConfig:
    return AgentConfig(id=agent_id, nome=agent_id, factory_ia_model="ollama", model="m", descricao="d", prompt="p")


def _setup() -> tuple[OrquestradorController, _Agents, _Teams, RecordingLogger]:
    logger = RecordingLogger()
    runtime = FakeAgentRuntime()
    agents = _Agents([_agent("a1"), _agent("a2")])
    teams = _Teams([TeamConfig(id="t1", nome="t1", factory_ia_model="ollama", model="m", member_ids=["a1"])])
    controller = OrquestradorController(
        get_active_agents_use_case=GetActiveAgentsUseCase(runtime, agents, logger),
        get_active_teams_use_case=GetActiveTeamsUseCase(runtime, teams, logger),
        logger=logger,
    )
    return controller, agents, teams, logger


@pytest.mark.parametrize("failing", ["agents", "teams"])
async def test_refresh_que_falha_mantem_o_cache_anterior_loga_e_levanta(failing: str) -> None:
    controller, agents, teams, logger = _setup()
    await controller.warm_up_cache()
    before_agents = await controller.get_agents()
    before_teams = await controller.get_teams()
    (agents if failing == "agents" else teams).broken = True

    with pytest.raises(RuntimeError, match="fora do ar"):
        await controller.refresh_agents()

    stats = controller.get_cache_stats()
    assert (stats["agents"]["status"], stats["agents"]["agent_count"]) == ("active", 2)
    assert (stats["teams"]["status"], stats["teams"]["team_count"]) == ("active", 1)
    assert await controller.get_agents() is before_agents
    assert await controller.get_teams() is before_teams
    assert ("Falha ao atualizar o cache de agentes e teams; mantido o anterior", {"error_type": "RuntimeError"}) in [
        (r.message, r.context) for r in logger.records if r.level == "error"
    ]
    assert "Cache de agentes e teams atualizado" not in logger.messages("info")


async def test_refresh_com_sucesso_troca_os_dois_caches() -> None:
    controller, agents, _, logger = _setup()
    await controller.warm_up_cache()
    old_agents = await controller.get_agents()
    agents._items.append(_agent("a3"))

    await controller.refresh_agents()

    new_agents = await controller.get_agents()
    assert new_agents is not old_agents and [a.id for a in new_agents] == ["a1", "a2", "a3"]
    assert controller.get_cache_stats()["teams"]["team_count"] == 1
    assert "Cache de agentes e teams atualizado" in logger.messages("info")


async def test_refresh_sem_cache_anterior_que_falha_deixa_vazio_e_levanta() -> None:
    controller, agents, _, _ = _setup()
    agents.broken = True

    with pytest.raises(RuntimeError):
        await controller.refresh_agents()

    assert controller.get_cache_stats() == {"agents": {"status": "empty"}, "teams": {"status": "empty"}}
