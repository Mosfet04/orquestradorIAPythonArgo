"""Testes unitários para AppConfig."""

from __future__ import annotations

import os
from unittest.mock import patch

import pytest

from src.infrastructure.config.app_config import AppConfig


class TestAppConfig:
    def test_load_with_defaults(self):
        with patch.dict(os.environ, {}, clear=True):
            config = AppConfig.load()
        assert config.mongo_database_name == "agno"
        assert config.app_port == 7777

    def test_load_from_env(self):
        env = {
            "MONGO_CONNECTION_STRING": "mongodb://custom:9999",
            "MONGO_DATABASE_NAME": "mydb",
            "APP_TITLE": "Custom",
            "APP_HOST": "127.0.0.1",
            "APP_PORT": "8888",
            "LOG_LEVEL": "DEBUG",
            "OLLAMA_BASE_URL": "http://ollama:11434",
        }
        with patch.dict(os.environ, env, clear=True):
            config = AppConfig.load()
        assert config.mongo_connection_string == "mongodb://custom:9999"
        assert config.app_port == 8888
        assert config.log_level == "DEBUG"

    def test_frozen_cannot_change(self):
        config = AppConfig.load()
        with pytest.raises(AttributeError):
            config.app_port = 1234  # type: ignore[misc]

    def test_validate_empty_connection_string(self):
        with patch.dict(os.environ, {"MONGO_CONNECTION_STRING": ""}, clear=True):
            with pytest.raises(ValueError, match="MONGO_CONNECTION_STRING"):
                AppConfig.load()

    def test_validate_empty_database_name(self):
        with patch.dict(
            os.environ,
            {"MONGO_CONNECTION_STRING": "mongodb://x", "MONGO_DATABASE_NAME": ""},
            clear=True,
        ):
            with pytest.raises(ValueError, match="MONGO_DATABASE_NAME"):
                AppConfig.load()
