"""Port para geração de sumários via LLM."""

from __future__ import annotations

from abc import ABC, abstractmethod


class SummaryTimeoutError(TimeoutError):
    """O modelo de sumário não respondeu no prazo.

    Sinal para o chamador usar o fallback e não insistir no mesmo lote (ex.: os demais nós
    do documento): um modelo travado custaria um timeout por chamada.
    """


class ISummaryGenerator(ABC):
    """Interface para gerar resumos de trechos de texto."""

    @abstractmethod
    async def generate_summary(self, content: str) -> str:
        """Gera um resumo conciso do conteúdo fornecido.

        Pode levantar ``SummaryTimeoutError``; outros erros são tratados pela implementação
        ou sobem para o chamador, que usa o fallback.
        """
        ...
