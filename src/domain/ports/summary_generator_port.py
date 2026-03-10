"""Port para geração de sumários via LLM."""

from __future__ import annotations

from abc import ABC, abstractmethod


class ISummaryGenerator(ABC):
    """Interface para gerar resumos de trechos de texto."""

    @abstractmethod
    async def generate_summary(self, content: str) -> str:
        """Gera um resumo conciso do conteúdo fornecido."""
        ...
