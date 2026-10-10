"""Runtime de agentes falso (porta ``AgentRuntime``, F2-04): sem framework, sem Mongo, sem modelo.

Devolve handles com o id da configuração, registra cada pedido (com a thread em que rodou) e
falha nos ids pedidos com a exceção dada. Serve para testar a política de carga dos casos de
uso (id repetido, isolamento de falha, motivo no log, I/O fora do event loop).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.team_config import TeamConfig
from src.domain.ports import AgentHandle, AgentRuntime
from tests.fakes.models import running_on_event_loop


@dataclass(frozen=True)
class FakeAgentHandle:
    id: str
    nome: str


@dataclass(frozen=True)
class FakeTeamHandle:
    id: str
    nome: str
    member_ids: tuple[str, ...]


@dataclass(frozen=True)
class TeamBuild:
    """Um pedido de ``build_team``: config, ids dos agentes recebidos e se rodou no event loop."""

    config: TeamConfig
    agent_ids: tuple[str, ...]
    on_event_loop: bool


class FakeAgentRuntime(AgentRuntime):
    """Monta handles em memória; ``failures`` mapeia id -> exceção levantada ao montar."""

    def __init__(self, *, failures: Mapping[str, BaseException] | None = None) -> None:
        self._failures = dict(failures or {})
        self.agent_builds: list[AgentConfig] = []
        self.team_builds: list[TeamBuild] = []

    async def build_agent(self, config: AgentConfig) -> FakeAgentHandle:
        self.agent_builds.append(config)
        if config.id in self._failures:
            raise self._failures[config.id]
        return FakeAgentHandle(id=config.id, nome=config.nome)

    def build_team(self, config: TeamConfig, agents: Sequence[AgentHandle]) -> FakeTeamHandle:
        agent_ids = tuple(agent.id for agent in agents)
        self.team_builds.append(TeamBuild(config, agent_ids, running_on_event_loop()))
        if config.id in self._failures:
            raise self._failures[config.id]
        members = tuple(member for member in config.member_ids if member in agent_ids)
        if not members:  # como o AgnoRuntime: team sem membro válido não sobe
            raise ValueError(f"Team {config.id!r}: nenhum membro válido encontrado")
        return FakeTeamHandle(id=config.id, nome=config.nome, member_ids=members)
