"""Testes para AppFactory."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.infrastructure.web.app_factory import AppFactory


class TestAppFactory:
    @patch("src.infrastructure.web.app_factory.DependencyContainer")
    @patch("src.infrastructure.web.app_factory.AppConfig")
    async def test_create_app_returns_fastapi(self, mock_config_cls, mock_container_cls):
        mock_config_cls.load.return_value = MagicMock()

        mock_controller = MagicMock()
        mock_controller.get_agents = AsyncMock(return_value=[])
        mock_controller.get_cache_stats = MagicMock(return_value={})
        mock_controller.refresh_agents = AsyncMock()
        mock_controller.warm_up_cache = AsyncMock()

        mock_container = MagicMock()
        mock_container.get_orquestrador_controller.return_value = mock_controller
        mock_container.health_service = MagicMock()
        mock_container.cleanup = AsyncMock()

        mock_container_cls.create_async = AsyncMock(return_value=mock_container)

        factory = AppFactory()
        app = await factory.create_app()
        assert app is not None

    @patch("agno.os.AgentOS")
    @patch("src.infrastructure.web.app_factory.DependencyContainer")
    @patch("src.infrastructure.web.app_factory.AppConfig")
    async def test_create_app_with_agents_uses_agentos(
        self, mock_config_cls, mock_container_cls, mock_os
    ):
        mock_config_cls.load.return_value = MagicMock()
        mock_agent = MagicMock()

        mock_controller = MagicMock()
        mock_controller.get_agents = AsyncMock(return_value=[mock_agent])
        mock_controller.warm_up_cache = AsyncMock()

        mock_container = MagicMock()
        mock_container.get_orquestrador_controller.return_value = mock_controller
        mock_container.cleanup = AsyncMock()

        mock_container_cls.create_async = AsyncMock(return_value=mock_container)

        mock_os_inst = MagicMock()
        mock_os_inst.get_app.return_value = MagicMock()
        mock_os.return_value = mock_os_inst

        factory = AppFactory()
        app = await factory.create_app()
        mock_os.assert_called_once()
        assert app is not None
