"""Port para parsing de documentos em árvore hierárquica."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import List

from src.domain.entities.document_node import DocumentNode


class IDocumentParser(ABC):
    """Interface para transformar texto bruto em nós hierárquicos."""

    @abstractmethod
    def parse(self, content: str, doc_name: str) -> List[DocumentNode]:
        """Parseia o conteúdo de um documento e retorna lista de ``DocumentNode``.

        Os nós retornados já possuem ``parent_id`` e ``children_ids``
        corretamente vinculados.
        """
        ...
