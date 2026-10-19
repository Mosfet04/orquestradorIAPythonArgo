"""Porta do repositório de configurações de agentes."""

from abc import ABC, abstractmethod
from typing import List

from src.domain.entities.agent_config import AgentConfig
from src.domain.repositories.versioning import Versioned


class IAgentConfigRepository(ABC):
    """Interface para repositório de configurações de agentes."""

    @abstractmethod
    async def get_active_agents(self) -> List[AgentConfig]:
        """Retorna lista de agentes ativos."""
        ...

    @abstractmethod
    async def get_agent_by_id(self, agent_id: str) -> AgentConfig:
        """Retorna um agente por ID."""
        ...

    @abstractmethod
    async def list_all(self) -> List[Versioned[AgentConfig]]:
        """Todos os documentos, inclusive inativos e ids repetidos, na ordem estável, com a versão."""
        ...

    @abstractmethod
    async def save(self, entity: AgentConfig, expected_version: int) -> int:
        """Grava (cria ou substitui) o documento do id e devolve a nova versão.

        ``ConfigVersionConflict`` se o documento não estiver em ``expected_version`` (0 = id novo ou
        documento legado); ``ConfigStoreReadOnly`` se o store não aceitar escrita.
        """
        ...

    @abstractmethod
    async def delete(self, entity_id: str, expected_version: int) -> None:
        """Remove o documento do id; mesmos erros do ``save``."""
        ...
