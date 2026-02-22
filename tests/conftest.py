"""Configurações compartilhadas para todos os testes."""

from __future__ import annotations

import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.fixture(scope="session", autouse=True)
def setup_test_environment():
    """Configura variáveis de ambiente para testes."""
    test_env = {
        "MONGO_CONNECTION_STRING": os.getenv(
            "MONGO_CONNECTION_STRING", "mongodb://localhost:27017"
        ),
        "MONGO_DATABASE_NAME": os.getenv("MONGO_DATABASE_NAME", "testdb"),
        "APP_TITLE": "Orquestrador de Agentes IA",
        "LOG_LEVEL": "ERROR",
    }
    with patch.dict(os.environ, test_env):
        yield


@pytest.fixture
def mock_logger():
    """Mock para ILogger."""
    logger = MagicMock()
    logger.info = MagicMock()
    logger.warning = MagicMock()
    logger.error = MagicMock()
    logger.debug = MagicMock()
    return logger


@pytest.fixture
def mock_agent_config_repository():
    """Mock assíncrono para IAgentConfigRepository."""
    repo = AsyncMock()
    repo.get_active_agents = AsyncMock(return_value=[])
    repo.get_agent_by_id = AsyncMock(return_value=None)
    return repo


@pytest.fixture
def mock_tool_repository():
    """Mock assíncrono para IToolRepository."""
    repo = AsyncMock()
    repo.get_tools_by_ids = AsyncMock(return_value=[])
    repo.get_all_active_tools = AsyncMock(return_value=[])
    repo.get_tool_by_id = AsyncMock(return_value=None)
    return repo
