"""Use case: obter teams ativos."""

from __future__ import annotations

from typing import List

from agno.agent import Agent
from agno.team import Team

from src.application.services.team_factory_service import TeamFactoryService
from src.domain.ports import ILogger
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
        configs = await self._repository.get_active_teams()
        if not configs:
            return []

        teams: List[Team] = []
        for config in configs:
            try:
                team = self._factory.create_team(config, agents)
                teams.append(team)
            except Exception as exc:
                self._logger.error(
                    "Erro ao criar team",
                    team_id=config.id,
                    error=str(exc),
                )
        return teams
