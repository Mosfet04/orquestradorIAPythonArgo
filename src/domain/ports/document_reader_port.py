"""Port para leitura de documentos (texto, PDF, etc.)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Optional


class IDocumentReader(ABC):
    """Interface para leitura de documentos do sistema de arquivos."""

    @abstractmethod
    def read(self, doc_name: str) -> Optional[str]:
        """Lê um documento pelo nome e retorna seu conteúdo textual.

        Parameters
        ----------
        doc_name:
            Nome do documento (ex: ``basic-prog.txt``, ``manual.pdf``).

        Returns
        -------
        Optional[str]
            Conteúdo textual do documento, ou ``None`` se não foi
            possível extrair texto.

        Raises
        ------
        FileNotFoundError
            Se o documento não existir no storage.
        """
        ...
