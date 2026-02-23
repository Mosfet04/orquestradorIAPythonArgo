"""Interface para repositório de configurações de teams."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from src.domain.entities.team_config import TeamConfig


class ITeamConfigRepository(ABC):
    """Interface para repositório de configurações de teams."""

    @abstractmethod
    async def get_active_teams(self) -> List[TeamConfig]:
        """Retorna lista de teams ativos."""
        ...

    @abstractmethod
    async def get_team_by_id(self, team_id: str) -> Optional[TeamConfig]:
        """Retorna um team por ID."""
        ...
