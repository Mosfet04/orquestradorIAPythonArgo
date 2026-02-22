from abc import ABC, abstractmethod
from typing import Any, List
from src.domain.entities.tool import Tool


class IToolFactory(ABC):
    """Interface para criação de ferramentas HTTP compatíveis com o framework de agentes."""

    @abstractmethod
    async def create_tools_from_configs(self, tools: List[Tool]) -> List[Any]:
        ...
