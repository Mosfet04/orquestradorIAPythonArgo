"""``AgnoRuntime``: implementação da porta ``AgentRuntime`` com o agno 2.5 (F2-04)."""

from __future__ import annotations

from collections.abc import Sequence
from typing import cast

from agno.agent import Agent

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.team_config import TeamConfig
from src.domain.ports import AgentHandle, AgentRuntime, TeamHandle
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService
from src.infrastructure.runtime.agno.team_factory_service import TeamFactoryService


class AgnoRuntime(AgentRuntime):
    """Monta ``agno.Agent``/``agno.Team`` pelas fábricas; os handles são os próprios objetos do agno.

    Por isso a infraestrutura do mesmo runtime (montagem do AgentOS, rotas AG-UI e de cancel)
    usa os handles direto. ``Agent.id``/``Team.id`` são ``Optional[str]`` no agno; aqui são
    sempre o id da configuração (texto validado no domínio).
    """

    def __init__(self, *, agent_factory: AgentFactoryService, team_factory: TeamFactoryService) -> None:
        self._agent_factory = agent_factory
        self._team_factory = team_factory

    async def build_agent(self, config: AgentConfig) -> AgentHandle:
        return cast(AgentHandle, await self._agent_factory.create_agent(config))

    def build_team(self, config: TeamConfig, agents: Sequence[AgentHandle]) -> TeamHandle:
        # Os handles de agente deste runtime são ``agno.Agent`` (criados por ``build_agent``).
        members = cast(list[Agent], list(agents))
        return cast(TeamHandle, self._team_factory.create_team(config, members))
