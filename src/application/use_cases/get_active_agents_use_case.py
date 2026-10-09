"""Use case: obter agentes ativos."""

from __future__ import annotations

import asyncio

from src.domain.entities.agent_config import AgentConfig
from src.domain.ports import AgentHandle, AgentRuntime, ILogger, InvalidModelConfigError
from src.domain.repositories.agent_config_repository import IAgentConfigRepository


class GetActiveAgentsUseCase:
    """Busca configurações ativas e monta os agentes pelo runtime.

    A política de carga fica aqui (o runtime monta um agente por vez): um agente por id,
    montagem em paralelo e falha de um isolada dos outros, com um log por falha.
    """

    def __init__(
        self,
        runtime: AgentRuntime,
        agent_config_repository: IAgentConfigRepository,
        logger: ILogger,
    ) -> None:
        self._runtime = runtime
        self._repository = agent_config_repository
        self._logger = logger

    async def execute(self) -> list[AgentHandle]:
        """Busca configs e cria agentes em paralelo; falha de um não derruba os outros."""
        configs = self._unique_ids(await self._repository.get_active_agents())
        if not configs:
            return []

        tasks = [self._runtime.build_agent(cfg) for cfg in configs]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        agents: list[AgentHandle] = []
        for config, result in zip(configs, results, strict=True):
            if isinstance(result, BaseException):
                self._log_failure(config.id, result)
                continue
            agents.append(result)
        return agents

    def _unique_ids(self, configs: list[AgentConfig]) -> list[AgentConfig]:
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
