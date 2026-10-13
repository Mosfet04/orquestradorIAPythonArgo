"""Porta de criação de modelo de chat a partir de ``ModelConfig`` (F2-02)."""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Protocol

from src.domain.entities.model_config import ModelConfig


class InvalidModelConfigError(ValueError):
    """Config de modelo/embedder recusada pela fábrica (provider, destino, params, chave, SDK).

    A mensagem é só texto nosso (provider, nome de variável, pacote a instalar, tipo do erro),
    nunca texto de exceção de SDK nem valor de segredo, e não encadeia a exceção original: pode
    ir ao log como motivo, como a de ``DocumentPathError``.
    """


class ChatModel(Protocol):
    """Modelo de chat opaco para o domínio; quem consome é o runtime que o criou."""

    id: str


class IModelFactory(ABC):
    """Cria o modelo de chat de uma ``ModelConfig``."""

    @abstractmethod
    def create_model(self, config: ModelConfig) -> ChatModel:
        """Modelo pronto para o runtime; config recusada levanta ``InvalidModelConfigError``.

        Pode fazer I/O bloqueante (segredo ``file:``, DNS do destino): no caminho async,
        chame via ``asyncio.to_thread``.
        """
