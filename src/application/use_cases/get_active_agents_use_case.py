"""Use case: obter agentes ativos."""

from __future__ import annotations

import asyncio
from typing import List

from agno.agent import Agent

from src.application.services.agent_factory_service import AgentFactoryService
from src.domain.repositories.agent_config_repository import IAgentConfigRepository


class GetActiveAgentsUseCase:
    """Busca configurações ativas e cria os agentes."""

    def __init__(
        self,
        agent_factory_service: AgentFactoryService,
        agent_config_repository: IAgentConfigRepository,
    ) -> None:
        self._factory = agent_factory_service
        self._repository = agent_config_repository

    async def execute(self) -> List[Agent]:
        """Busca configs e cria agentes em paralelo."""
        configs = await self._repository.get_active_agents()
        if not configs:
            return []

        tasks = [self._factory.create_agent(cfg) for cfg in configs]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        agents: List[Agent] = []
        for i, result in enumerate(results):
            if isinstance(result, Exception):
                # Log já ocorre dentro de AgentFactoryService
                continue
            agents.append(result)
        return agents