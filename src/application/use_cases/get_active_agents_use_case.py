"""Use case: obter agentes ativos."""

from __future__ import annotations

import asyncio

from src.application.use_cases.config_change import ConfigChange, ConfigNotBuildable
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

    async def execute(self, change: ConfigChange[AgentConfig] | None = None) -> list[AgentHandle]:
        """Busca configs e cria agentes em paralelo; falha de um não derruba os outros.

        Com ``change`` (escrita pela API, FA-04), monta a config ativa com a mudança aplicada e, se o agente
        alterado não monta, levanta ``ConfigNotBuildable`` depois de logar as falhas.
        """
        active = await self._repository.get_active_agents()
        configs = self._unique_ids(change.applied(active) if change is not None else active)
        if not configs:
            return []

        tasks = [self._runtime.build_agent(cfg) for cfg in configs]
        results = await asyncio.gather(*tasks, return_exceptions=True)

        agents: list[AgentHandle] = []
        refusal: ConfigNotBuildable | None = None
        for config, result in zip(configs, results, strict=True):
            if isinstance(result, BaseException):
                self._log_failure(config.id, result)
                if change is not None and config.id == change.entity_id:
                    refusal = ConfigNotBuildable(type(result).__name__)
                continue
            agents.append(result)
        if refusal is not None:
            raise refusal
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
