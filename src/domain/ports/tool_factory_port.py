"""Porta da fábrica de tools HTTP para o runtime de agentes."""

from abc import ABC, abstractmethod
from typing import Any, List

from src.domain.entities.tool import Tool


class IToolFactory(ABC):
    """Interface para criação de ferramentas HTTP compatíveis com o framework de agentes."""

    @abstractmethod
    async def create_tools_from_configs(self, tools: List[Tool]) -> List[Any]:
        """Cria as tools do runtime a partir das configurações HTTP."""
        ...
