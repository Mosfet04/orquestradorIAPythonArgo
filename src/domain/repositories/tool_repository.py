from abc import ABC, abstractmethod
from typing import List
from ..entities.tool import Tool


class IToolRepository(ABC):
    """Interface para repositório de tools."""
    
    @abstractmethod
    async def get_tools_by_ids(self, tool_ids: List[str]) -> List[Tool]:
        """Retorna lista de tools pelos IDs fornecidos."""
        ...
    
    @abstractmethod
    async def get_tool_by_id(self, tool_id: str) -> Tool:
        """Retorna uma tool por ID."""
        ...
    
    @abstractmethod
    async def get_all_active_tools(self) -> List[Tool]:
        """Retorna todas as tools ativas."""
        ...
