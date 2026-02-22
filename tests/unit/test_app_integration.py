"""Testes de integração leve para AppFactory + endpoints admin."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from src.infrastructure.web.app_factory import AppFactory


@pytest.fixture
def mock_container():
    container = MagicMock()
    controller = MagicMock()
    controller.get_agents = AsyncMock(return_value=[])
    controller.get_cache_stats = MagicMock(return_value={"hits": 0, "misses": 0})
    controller.warm_up_cache = AsyncMock()
    controller.refresh_agents = AsyncMock()
    container.get_orquestrador_controller.return_value = controller
    container.health_service = None
    container.cleanup = AsyncMock()
    return container


class TestAppFactory:
    async def test_create_app_no_agents(self, mock_container):
        """Sem agentes, retorna FastAPI base (sem AgentOS)."""
        factory = AppFactory()
        with patch.object(
            AppFactory, "_lifespan", return_value=_null_lifespan()
        ):
            factory._container = mock_container
            app = await factory.create_app()
            assert app is not None

    async def test_health_endpoint(self, mock_container):
        factory = AppFactory()
        with patch.object(
            AppFactory, "_lifespan", return_value=_null_lifespan()
        ):
            factory._container = mock_container
            app = await factory.create_app()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "healthy"

    async def test_cache_metrics_endpoint(self, mock_container):
        factory = AppFactory()
        with patch.object(
            AppFactory, "_lifespan", return_value=_null_lifespan()
        ):
            factory._container = mock_container
            app = await factory.create_app()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/metrics/cache")
            assert resp.status_code == 200

    async def test_refresh_cache_endpoint(self, mock_container):
        factory = AppFactory()
        with patch.object(
            AppFactory, "_lifespan", return_value=_null_lifespan()
        ):
            factory._container = mock_container
            app = await factory.create_app()

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/admin/refresh-cache")
            assert resp.status_code == 200
            assert resp.json()["status"] == "cache_refreshed"


# ── helpers ────────────────────────────────────────────────────────

from contextlib import asynccontextmanager  # noqa: E402


@asynccontextmanager
async def _null_lifespan(*_args, **_kwargs):
    yield
