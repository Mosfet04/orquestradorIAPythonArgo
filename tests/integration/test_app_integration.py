"""Testes de integração para a aplicação."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.infrastructure.web.app_factory import AppFactory


class TestIntegrationApp:
    @patch("src.infrastructure.web.app_factory.DependencyContainer")
    @patch("src.infrastructure.web.app_factory.AppConfig")
    async def test_app_creation(self, mock_config_cls, mock_container_cls):
        mock_config_cls.load.return_value = MagicMock()
        mock_controller = MagicMock()
        mock_controller.get_agents = AsyncMock(return_value=[])
        mock_controller.warm_up_cache = AsyncMock()
        mock_container = MagicMock()
        mock_container.get_orquestrador_controller.return_value = mock_controller
        mock_container.health_service = None
        mock_container.cleanup = AsyncMock()
        mock_container_cls.create_async = AsyncMock(return_value=mock_container)

        factory = AppFactory()
        app = await factory.create_app()
        assert app is not None
