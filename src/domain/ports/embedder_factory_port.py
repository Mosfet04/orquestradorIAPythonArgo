"""Porta de criação de embedder a partir de ``ModelConfig`` (F2-02)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol

from src.domain.entities.model_config import ModelConfig


class TextEmbedder(Protocol):
    """Embedder opaco para o domínio: só o que a indexação usa."""

    def get_embedding(self, text: str) -> list[float]:
        """Vetor do texto (síncrono e com rede no provider real: chame fora do event loop)."""
        ...


class IEmbedderFactory(ABC):
    """Cria o embedder de uma ``ModelConfig``."""

    @abstractmethod
    def create_embedder(self, config: ModelConfig) -> TextEmbedder:
        """Embedder pronto; config recusada levanta ``InvalidModelConfigError``.

        Pode fazer I/O bloqueante (segredo ``file:``, DNS do destino): no caminho async,
        chame via ``asyncio.to_thread``.
        """
