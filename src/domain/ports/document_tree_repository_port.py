"""Port para persistência da árvore hierárquica de documentos."""

from __future__ import annotations

from abc import ABC, abstractmethod

from src.domain.entities.document_node import DocumentNode


class IDocumentTreeRepository(ABC):
    """Interface para armazenar e consultar nós da árvore de documentos.

    Contrato (``tests/contract/test_document_tree_ordering_contract.py``):
    - ordem de leitura = ordem de gravação: dentro de um lote de ``save_nodes``, a da lista; entre
      lotes, o anterior antes do posterior;
    - id de nó repetido (no mesmo lote ou contra nó já gravado): vence o primeiro gravado; o repetido
      é ignorado com log de erro e os demais nós do lote são gravados, sem levantar.
    """

    @abstractmethod
    async def save_nodes(self, nodes: list[DocumentNode]) -> None:
        """Persiste uma lista de nós (insert em lote), depois dos lotes anteriores."""
        ...

    @abstractmethod
    async def get_root_nodes(self, doc_name: str) -> list[DocumentNode]:
        """Retorna os nós raiz (level 0) de um documento, na ordem de gravação."""
        ...

    @abstractmethod
    async def get_children(self, parent_id: str) -> list[DocumentNode]:
        """Retorna os filhos diretos de um nó, na ordem de gravação."""
        ...

    @abstractmethod
    async def get_node(self, node_id: str) -> DocumentNode | None:
        """Retorna um nó pelo ID (o primeiro gravado com ele)."""
        ...

    @abstractmethod
    async def exists(self, doc_name: str) -> bool:
        """Verifica se já existem nós indexados para o documento."""
        ...

    @abstractmethod
    async def has_empty_embedding(self, doc_name: str) -> bool:
        """``True`` se algum nó do documento tem ``embedding`` igual a lista vazia (F2-11).

        ``None`` (nó sem texto) não conta: só o ``[]`` que a indexação de antes gravava.
        """
        ...

    @abstractmethod
    async def delete_document(self, doc_name: str) -> int:
        """Apaga todos os nós do documento (para reindexar); devolve quantos apagou."""
        ...
