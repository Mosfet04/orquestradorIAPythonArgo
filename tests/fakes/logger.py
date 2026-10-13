"""``ILogger`` em memória para asserções sobre o que foi logado."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from src.domain.ports import ILogger


@dataclass(frozen=True)
class LoggedRecord:
    level: str
    message: str
    context: dict[str, Any] = field(default_factory=dict)


class RecordingLogger(ILogger):
    """Guarda cada chamada em ``records`` na ordem em que aconteceu."""

    def __init__(self) -> None:
        self.records: list[LoggedRecord] = []

    def _log(self, level: str, message: str, kwargs: dict[str, Any]) -> None:
        self.records.append(LoggedRecord(level=level, message=message, context=dict(kwargs)))

    def info(self, message: str, **kwargs: Any) -> None:
        self._log("info", message, kwargs)

    def warning(self, message: str, **kwargs: Any) -> None:
        self._log("warning", message, kwargs)

    def error(self, message: str, **kwargs: Any) -> None:
        self._log("error", message, kwargs)

    def debug(self, message: str, **kwargs: Any) -> None:
        self._log("debug", message, kwargs)

    def messages(self, *levels: str) -> list[str]:
        """Mensagens dos níveis pedidos (todas, se nenhum nível for passado)."""
        return [r.message for r in self.records if not levels or r.level in levels]
