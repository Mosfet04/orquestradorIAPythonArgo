"""Adapter que implementa ILogger usando structlog."""

from __future__ import annotations

from typing import Any

from src.domain.ports import ILogger
from src.infrastructure.logging.structlog_logger import LoggerFactory


class StructlogLoggerAdapter(ILogger):
    """Wraps structlog para satisfazer a porta ILogger."""

    def __init__(self, name: str = "app") -> None:
        self._logger = LoggerFactory.get_logger(name)

    def info(self, msg: str, **kwargs: Any) -> None:
        self._logger.info(msg, **kwargs)

    def warning(self, msg: str, **kwargs: Any) -> None:
        self._logger.warning(msg, **kwargs)

    def error(self, msg: str, **kwargs: Any) -> None:
        self._logger.error(msg, **kwargs)

    def debug(self, msg: str, **kwargs: Any) -> None:
        self._logger.debug(msg, **kwargs)
