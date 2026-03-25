"""Port para persistência da árvore hierárquica de documentos."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List, Optional

from src.domain.entities.document_node import DocumentNode


class IDocumentTreeRepository(ABC):
    """Interface para armazenar e consultar nós da árvore de documentos."""

    @abstractmethod
    async def save_nodes(self, nodes: List[DocumentNode]) -> None:
        """Persiste uma lista de nós (insert em lote)."""
        ...

    @abstractmethod
    async def get_root_nodes(self, doc_name: str) -> List[DocumentNode]:
        """Retorna os nós raiz (level 0) de um documento."""
        ...

    @abstractmethod
    async def get_children(self, parent_id: str) -> List[DocumentNode]:
        """Retorna os filhos diretos de um nó."""
        ...

    @abstractmethod
    async def get_children_batch(self, parent_ids: List[str]) -> List[DocumentNode]:
        """Retorna os filhos diretos de vários nós em uma única query."""
        ...

    @abstractmethod
    async def get_node(self, node_id: str) -> Optional[DocumentNode]:
        """Retorna um nó pelo ID."""
        ...

    @abstractmethod
    async def exists(self, doc_name: str) -> bool:
        """Verifica se já existem nós indexados para o documento."""
        ...

    @abstractmethod
    async def delete_by_doc_name(self, doc_name: str) -> int:
        """Remove todos os nós de um documento. Retorna quantidade removida."""
        ...

    @abstractmethod
    async def replace_nodes(self, doc_name: str, nodes: List[DocumentNode]) -> None:
        """Remove nós existentes e insere novos atomicamente (quando possível)."""
        ...
