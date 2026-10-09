"""Use case: obter agentes ativos."""

from __future__ import annotations

import asyncio
from typing import List

from agno.agent import Agent

from src.application.services.agent_factory_service import AgentFactoryService
from src.domain.entities.agent_config import AgentConfig
from src.domain.ports import ILogger
from src.domain.ports.model_factory_port import InvalidModelConfigError
from src.domain.repositories.agent_config_repository import IAgentConfigRepository


class GetActiveAgentsUseCase:
    """Busca configurações ativas e cria os agentes."""

    def __init__(
        self,
        agent_factory_service: AgentFactoryService,
        agent_config_repository: IAgentConfigRepository,
        logger: ILogger,
    ) -> None:
        self._factory = agent_factory_service
        self._repository = agent_config_repository
        self._logger = logger

    async def execute(self) -> List[Agent]:
        """Busca configs e cria agentes em paralelo; falha de um não derruba os outros."""
        configs = self._unique_ids(await self._repository.get_active_agents())
        if not configs:
            return []

        tasks = [self._factory.create_agent(cfg) for cfg in configs]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        agents: List[Agent] = []
        for config, result in zip(configs, results, strict=True):
            if isinstance(result, BaseException):
                self._log_failure(config.id, result)
                continue
            agents.append(result)
        return agents

    def _unique_ids(self, configs: List[AgentConfig]) -> List[AgentConfig]:
        """Um agente por id; o primeiro vence (o AgentOS recusa a montagem com id repetido)."""
        unique: dict[str, AgentConfig] = {}
        for config in configs:
            if config.id in unique:
                self._logger.error("Agente com id repetido ignorado", agent_id=config.id)
                continue
            unique[config.id] = config
        return list(unique.values())

    def _log_failure(self, agent_id: str, exc: BaseException) -> None:
        # Só o tipo: texto de exceção de SDK pode trazer segredo. A recusa da validação do
        # modelo é texto nosso e vai como ``reason`` (como o do ``DocumentPathError``).
        reason = {"reason": str(exc)} if isinstance(exc, InvalidModelConfigError) else {}
        self._logger.error(
            "Agente não carregado", agent_id=agent_id, error_type=type(exc).__name__, **reason
        )
