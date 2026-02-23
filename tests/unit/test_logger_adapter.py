"""Testes para StructlogLoggerAdapter."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

from src.infrastructure.logging.logger_adapter import StructlogLoggerAdapter


class TestStructlogLoggerAdapter:
    @patch("src.infrastructure.logging.logger_adapter.LoggerFactory")
    def test_info(self, mock_factory):
        mock_log = MagicMock()
        mock_factory.get_logger.return_value = mock_log
        adapter = StructlogLoggerAdapter("test")
        adapter.info("hello", key="val")
        mock_log.info.assert_called_once_with("hello", key="val")

    @patch("src.infrastructure.logging.logger_adapter.LoggerFactory")
    def test_warning(self, mock_factory):
        mock_log = MagicMock()
        mock_factory.get_logger.return_value = mock_log
        adapter = StructlogLoggerAdapter("test")
        adapter.warning("warn msg", x=1)
        mock_log.warning.assert_called_once_with("warn msg", x=1)

    @patch("src.infrastructure.logging.logger_adapter.LoggerFactory")
    def test_error(self, mock_factory):
        mock_log = MagicMock()
        mock_factory.get_logger.return_value = mock_log
        adapter = StructlogLoggerAdapter("test")
        adapter.error("err", code=500)
        mock_log.error.assert_called_once_with("err", code=500)

    @patch("src.infrastructure.logging.logger_adapter.LoggerFactory")
    def test_debug(self, mock_factory):
        mock_log = MagicMock()
        mock_factory.get_logger.return_value = mock_log
        adapter = StructlogLoggerAdapter("test")
        adapter.debug("debug msg")
        mock_log.debug.assert_called_once_with("debug msg")
