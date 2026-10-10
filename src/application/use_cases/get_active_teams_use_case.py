"""Use case: obter teams ativos."""

from __future__ import annotations

import asyncio
from collections.abc import Sequence

from src.domain.entities.team_config import TeamConfig
from src.domain.ports import AgentHandle, AgentRuntime, ILogger, InvalidModelConfigError, TeamHandle
from src.domain.repositories.team_config_repository import ITeamConfigRepository


class GetActiveTeamsUseCase:
    """Busca configurações de teams ativas e monta os teams pelo runtime.

    A política de carga fica aqui (o runtime monta um team por vez): um team por id, montagem
    fora do event loop e falha de um isolada dos outros, com um log por falha.
    """

    def __init__(
        self,
        runtime: AgentRuntime,
        team_config_repository: ITeamConfigRepository,
        logger: ILogger,
    ) -> None:
        self._runtime = runtime
        self._repository = team_config_repository
        self._logger = logger

    async def execute(self, agents: Sequence[AgentHandle]) -> list[TeamHandle]:
        """Busca configs de teams e cria instâncias usando os agentes fornecidos.

        Args:
            agents: Agentes já montados pelo mesmo runtime (membros potenciais).

        Returns:
            Teams prontos para servir.
        """
        configs = self._unique_ids(await self._repository.get_active_teams())
        if not configs:
            return []

        teams: list[TeamHandle] = []
        for config in configs:
            try:
                # A criação do modelo pode ler segredo (file:) e resolver DNS: fora do loop.
                team = await asyncio.to_thread(self._runtime.build_team, config, agents)
                teams.append(team)
            # Isolamento por team: qualquer falha de um (inclusive de SDK) não derruba os outros.
            except Exception as exc:  # noqa: BLE001 - isolamento por team; logado abaixo
                # Só o tipo: texto de exceção de SDK pode trazer segredo. A recusa da config
                # do modelo é texto nosso e vai como ``reason`` (como no use case de agentes).
                reason = {"reason": str(exc)} if isinstance(exc, InvalidModelConfigError) else {}
                self._logger.error(
                    "Team não carregado",
                    team_id=config.id,
                    error_type=type(exc).__name__,
                    **reason,
                )
        return teams

    def _unique_ids(self, configs: list[TeamConfig]) -> list[TeamConfig]:
        """Um team por id; o primeiro vence (o AgentOS recusa a montagem com id repetido)."""
        unique: dict[str, TeamConfig] = {}
        for config in configs:
            if config.id in unique:
                self._logger.error("Team com id repetido ignorado", team_id=config.id)
                continue
            unique[config.id] = config
        return list(unique.values())
