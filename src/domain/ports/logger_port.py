"""Porta de logging usada pelas camadas internas."""

from abc import ABC, abstractmethod
from typing import Any


class ILogger(ABC):
    """Interface de logging para desacoplar a camada de aplicação da infraestrutura."""

    @abstractmethod
    def info(self, message: str, **kwargs: Any) -> None:
        """Registra mensagem informativa com contexto estruturado."""
        ...

    @abstractmethod
    def warning(self, message: str, **kwargs: Any) -> None:
        """Registra aviso com contexto estruturado."""
        ...

    @abstractmethod
    def error(self, message: str, **kwargs: Any) -> None:
        """Registra erro com contexto estruturado (nunca texto de exceção com segredo)."""
        ...

    @abstractmethod
    def debug(self, message: str, **kwargs: Any) -> None:
        """Registra mensagem de depuração com contexto estruturado."""
        ...
