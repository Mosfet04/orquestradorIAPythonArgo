from abc import ABC, abstractmethod
from typing import Any
from src.domain.entities.agent_config import AgentConfig


class IAgentBuilder(ABC):
    """Interface para construção de agentes a partir de configurações."""

    @abstractmethod
    def create_agent(self, config: AgentConfig) -> Any:
        """Cria um agente baseado na configuração fornecida."""
        ...

    @abstractmethod
    async def create_agent_async(self, config: AgentConfig) -> Any:
        """Versão assíncrona para criação de agentes."""
        ...
