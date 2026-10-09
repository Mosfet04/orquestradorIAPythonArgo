"""Use case: obter teams ativos."""

from __future__ import annotations

import asyncio
from typing import List

from agno.agent import Agent
from agno.team import Team

from src.application.services.team_factory_service import TeamFactoryService
from src.domain.entities.team_config import TeamConfig
from src.domain.ports import ILogger, InvalidModelConfigError
from src.domain.repositories.team_config_repository import ITeamConfigRepository


class GetActiveTeamsUseCase:
    """Busca configurações de teams ativas e cria os Teams agno."""

    def __init__(
        self,
        team_factory_service: TeamFactoryService,
        team_config_repository: ITeamConfigRepository,
        logger: ILogger,
    ) -> None:
        self._factory = team_factory_service
        self._repository = team_config_repository
        self._logger = logger

    async def execute(self, agents: List[Agent]) -> List[Team]:
        """Busca configs de teams e cria instâncias usando os agentes fornecidos.

        Args:
            agents: Lista de agentes já criados (usados como membros potenciais).

        Returns:
            Lista de Teams agno prontos para montar no AgentOS.
        """
        configs = self._unique_ids(await self._repository.get_active_teams())
        if not configs:
            return []

        teams: List[Team] = []
        for config in configs:
            try:
                # A criação do modelo pode ler segredo (file:) e resolver DNS: fora do loop.
                team = await asyncio.to_thread(self._factory.create_team, config, agents)
                teams.append(team)
            except Exception as exc:
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

    def _unique_ids(self, configs: List[TeamConfig]) -> List[TeamConfig]:
        """Um team por id; o primeiro vence (o AgentOS recusa a montagem com id repetido)."""
        unique: dict[str, TeamConfig] = {}
        for config in configs:
            if config.id in unique:
                self._logger.error("Team com id repetido ignorado", team_id=config.id)
                continue
            unique[config.id] = config
        return list(unique.values())
