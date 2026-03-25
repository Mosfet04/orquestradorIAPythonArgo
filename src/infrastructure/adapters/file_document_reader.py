"""Adapter de leitura de documentos do sistema de arquivos local."""

from __future__ import annotations

import os
from typing import Optional

from src.domain.ports.document_reader_port import IDocumentReader
from src.domain.ports.logger_port import ILogger


class FileDocumentReader(IDocumentReader):
    """Lê documentos ``.txt`` e ``.pdf`` a partir de um diretório base."""

    def __init__(self, *, base_dir: str = "docs", logger: ILogger) -> None:
        self._base_dir = base_dir
        self._logger = logger

    def read(self, doc_name: str) -> Optional[str]:
        """Lê documento pelo nome, resolvendo o path internamente."""
        doc_path = os.path.join(self._base_dir, doc_name)

        if doc_path.lower().endswith(".pdf"):
            return self._read_pdf(doc_path)

        return self._read_text(doc_path)

    def _read_text(self, doc_path: str) -> Optional[str]:
        try:
            with open(doc_path, "r", encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            raise
        except Exception as exc:
            self._logger.warning(
                "Erro ao ler documento texto",
                path=doc_path,
                error=str(exc),
            )
            return None

    def _read_pdf(self, doc_path: str) -> Optional[str]:
        try:
            import pdfplumber
        except ImportError as exc:
            self._logger.warning(
                "pdfplumber não instalado para leitura de PDF",
                error=str(exc),
            )
            return None

        try:
            with pdfplumber.open(doc_path) as pdf:
                pages = [page.extract_text() or "" for page in pdf.pages]
                content = "\n\n".join(filter(None, pages)).strip()
                return content or None
        except FileNotFoundError:
            raise
        except Exception as exc:
            self._logger.warning(
                "Erro ao ler documento PDF",
                path=doc_path,
                error=str(exc),
            )
            return None
