"""Interface para repositório de configurações de teams."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from src.domain.entities.team_config import TeamConfig
from src.domain.repositories.versioning import Versioned


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

    @abstractmethod
    async def list_all(self) -> List[Versioned[TeamConfig]]:
        """Todos os documentos, inclusive inativos e ids repetidos, na ordem estável, com a versão."""
        ...

    @abstractmethod
    async def save(self, entity: TeamConfig, expected_version: int) -> int:
        """Grava (cria ou substitui) o documento do id e devolve a nova versão.

        ``ConfigVersionConflict`` se o documento não estiver em ``expected_version`` (0 = id novo ou
        documento legado); ``ConfigStoreReadOnly`` se o store não aceitar escrita.
        """
        ...

    @abstractmethod
    async def delete(self, entity_id: str, expected_version: int) -> None:
        """Remove o documento do id; mesmos erros do ``save``."""
        ...
