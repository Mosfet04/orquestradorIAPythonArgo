"""Porta do repositório de tools."""

from abc import ABC, abstractmethod
from typing import List

from ..entities.tool import Tool
from .versioning import Versioned


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

    @abstractmethod
    async def list_all(self) -> List[Versioned[Tool]]:
        """Todos os documentos, inclusive inativos e ids repetidos, na ordem estável, com a versão."""
        ...

    @abstractmethod
    async def save(self, entity: Tool, expected_version: int) -> int:
        """Grava (cria ou substitui) o documento do id e devolve a nova versão.

        ``ConfigVersionConflict`` se o documento não estiver em ``expected_version`` (0 = id novo ou
        documento legado); ``ConfigStoreReadOnly`` se o store não aceitar escrita.
        """
        ...

    @abstractmethod
    async def delete(self, entity_id: str, expected_version: int) -> None:
        """Remove o documento do id; mesmos erros do ``save``."""
        ...
