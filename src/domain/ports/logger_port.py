from abc import ABC, abstractmethod
from typing import Any


class ILogger(ABC):
    """Interface de logging para desacoplar a camada de aplicação da infraestrutura."""

    @abstractmethod
    def info(self, message: str, **kwargs: Any) -> None:
        ...

    @abstractmethod
    def warning(self, message: str, **kwargs: Any) -> None:
        ...

    @abstractmethod
    def error(self, message: str, **kwargs: Any) -> None:
        ...

    @abstractmethod
    def debug(self, message: str, **kwargs: Any) -> None:
        ...
