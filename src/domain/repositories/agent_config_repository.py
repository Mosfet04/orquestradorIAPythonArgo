from abc import ABC, abstractmethod
from typing import List
from src.domain.entities.agent_config import AgentConfig


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
