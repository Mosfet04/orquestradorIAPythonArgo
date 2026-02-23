"""Testes estendidos para AppFactory — cobertura de middleware, lifespan e helpers."""

from __future__ import annotations

from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from src.infrastructure.web.app_factory import (
    AppFactory,
    _PlaygroundPrefixMiddleware,
    _AGENT_SESSION_RE,
    create_app,
)


# ── _PlaygroundPrefixMiddleware ──────────────────────────────────────


class TestPlaygroundPrefixMiddleware:
    """Testa o middleware de reescrita de paths."""

    async def test_strip_playground_prefix(self):
        """Paths /playground/… devem ser reescritos para /…"""
        factory = AppFactory()
        app = factory.create_app()
        factory._add_playground_rewrite(app)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/playground/admin/health")
            assert resp.status_code == 200

    async def test_rewrite_agent_sessions_path(self):
        """Paths /agents/{id}/sessions/… devem ser reescritos para /sessions/…"""
        m = _AGENT_SESSION_RE.match("/agents/my-agent/sessions/abc123")
        assert m is not None
        assert m.group(1) == "/sessions/abc123"

    async def test_no_rewrite_for_normal_path(self):
        """Paths normais não devem ser alterados."""
        m = _AGENT_SESSION_RE.match("/admin/health")
        assert m is None

    async def test_middleware_passthrough_no_playground(self):
        """Sem /playground, middleware não altera path."""
        factory = AppFactory()
        app = factory.create_app()
        factory._add_playground_rewrite(app)

        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "healthy"


# ── admin endpoints com container ────────────────────────────────────


class TestAdminEndpointsWithContainer:
    """Testa os endpoints admin quando _container está configurado."""

    @pytest.fixture
    def factory_with_container(self):
        factory = AppFactory()
        container = MagicMock()
        controller = MagicMock()
        controller.get_cache_stats = MagicMock(return_value={"agents": {"status": "active"}})
        controller.refresh_agents = AsyncMock()
        container.get_orquestrador_controller.return_value = controller
        container.health_service = MagicMock()
        container.health_service.check_async = AsyncMock(return_value={"status": "ok", "db": "connected"})
        container.cleanup = AsyncMock()
        factory._container = container
        return factory

    async def test_health_with_health_service(self, factory_with_container):
        """health_check deve chamar health_service quando disponível."""
        app = factory_with_container.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "ok"

    async def test_cache_metrics_with_container(self, factory_with_container):
        app = factory_with_container.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/metrics/cache")
            assert resp.status_code == 200
            assert resp.json()["agents"]["status"] == "active"

    async def test_refresh_cache_with_container(self, factory_with_container):
        app = factory_with_container.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/admin/refresh-cache")
            assert resp.status_code == 200
            assert resp.json()["status"] == "cache_refreshed"

    async def test_health_no_container(self):
        """Sem container, health_check retorna healthy padrão."""
        factory = AppFactory()
        app = factory.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/admin/health")
            assert resp.json()["status"] == "healthy"

    async def test_cache_metrics_no_container(self):
        """Sem container, retorna no_cache."""
        factory = AppFactory()
        app = factory.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.get("/metrics/cache")
            assert resp.json()["status"] == "no_cache"

    async def test_refresh_no_container(self):
        """Sem container, retorna no_cache."""
        factory = AppFactory()
        app = factory.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            resp = await client.post("/admin/refresh-cache")
            assert resp.json()["status"] == "no_cache"


# ── _ensure_container ────────────────────────────────────────────────


class TestEnsureContainer:
    @patch("src.infrastructure.web.app_factory.DependencyContainer")
    @patch("src.infrastructure.web.app_factory.AppConfig")
    async def test_ensure_container_creates(self, mock_config_cls, mock_dc_cls):
        mock_config = MagicMock()
        mock_config.mongo_database_name = "test_db"
        mock_config_cls.load.return_value = mock_config
        mock_dc_cls.create_async = AsyncMock(return_value=MagicMock())

        factory = AppFactory()
        assert factory._container is None

        await factory._ensure_container()
        assert factory._container is not None
        mock_config_cls.load.assert_called_once()
        mock_dc_cls.create_async.assert_awaited_once_with(mock_config)

    @patch("src.infrastructure.web.app_factory.DependencyContainer")
    @patch("src.infrastructure.web.app_factory.AppConfig")
    async def test_ensure_container_skips_if_exists(self, mock_config_cls, mock_dc_cls):
        factory = AppFactory()
        factory._container = MagicMock()  # já existe

        await factory._ensure_container()
        mock_config_cls.load.assert_not_called()


# ── _load_agents & _load_teams ───────────────────────────────────────


class TestLoadAgentsTeams:
    @pytest.fixture
    def factory_with_mocks(self):
        factory = AppFactory()
        controller = MagicMock()
        agent1 = MagicMock()
        agent1.id = "a1"
        agent2 = MagicMock()
        agent2.id = "a2"
        controller.warm_up_cache = AsyncMock()
        controller.get_agents = AsyncMock(return_value=[agent1, agent2])
        team1 = MagicMock()
        team1.id = "t1"
        controller.get_teams = AsyncMock(return_value=[team1])

        container = MagicMock()
        container.get_orquestrador_controller.return_value = controller
        factory._container = container
        return factory

    async def test_load_agents(self, factory_with_mocks):
        agents = await factory_with_mocks._load_agents()
        assert len(agents) == 2

    async def test_load_teams(self, factory_with_mocks):
        teams = await factory_with_mocks._load_teams()
        assert len(teams) == 1


# ── _mount_agent_os ──────────────────────────────────────────────────


class TestMountAgentOS:
    @patch("src.infrastructure.web.app_factory.AgentOS")
    @patch("src.infrastructure.web.app_factory.AGUI")
    async def test_mount_agent_os_success(self, mock_agui, mock_os_cls):
        factory = AppFactory()
        mock_agui.return_value = MagicMock()
        mock_os_instance = MagicMock()
        mock_os_instance.get_app.return_value = MagicMock()
        mock_os_cls.return_value = mock_os_instance

        from fastapi import FastAPI
        app = FastAPI()
        agents = [MagicMock(), MagicMock()]
        teams = [MagicMock()]

        factory._mount_agent_os(app, agents, teams)
        mock_os_cls.assert_called_once()
        mock_os_instance.get_app.assert_called_once()

    @patch("src.infrastructure.web.app_factory.AgentOS")
    @patch("src.infrastructure.web.app_factory.AGUI")
    async def test_mount_agent_os_empty_teams(self, mock_agui, mock_os_cls):
        factory = AppFactory()
        mock_agui.return_value = MagicMock()
        mock_os_instance = MagicMock()
        mock_os_instance.get_app.return_value = MagicMock()
        mock_os_cls.return_value = mock_os_instance

        from fastapi import FastAPI
        app = FastAPI()
        factory._mount_agent_os(app, [MagicMock()], [])
        # teams=[] → deve enviar None
        call_kwargs = mock_os_cls.call_args[1]
        assert call_kwargs.get("teams") is None


# ── _lifespan ────────────────────────────────────────────────────────


class TestLifespan:
    @patch("src.infrastructure.web.app_factory.AgentOS")
    @patch("src.infrastructure.web.app_factory.AGUI")
    @patch("src.infrastructure.web.app_factory.DependencyContainer")
    @patch("src.infrastructure.web.app_factory.AppConfig")
    async def test_lifespan_happy_path(self, mock_config_cls, mock_dc_cls, mock_agui, mock_os_cls):
        """Lifespan completo: container + agents + teams + mount."""
        factory = AppFactory()

        mock_config = MagicMock()
        mock_config.mongo_database_name = "db"
        mock_config_cls.load.return_value = mock_config

        controller = MagicMock()
        agent = MagicMock()
        agent.id = "a1"
        controller.warm_up_cache = AsyncMock()
        controller.get_agents = AsyncMock(return_value=[agent])
        controller.get_teams = AsyncMock(return_value=[])
        container = MagicMock()
        container.get_orquestrador_controller.return_value = controller
        container.cleanup = AsyncMock()
        mock_dc_cls.create_async = AsyncMock(return_value=container)

        mock_agui.return_value = MagicMock()
        mock_os_instance = MagicMock()
        mock_os_instance.get_app.return_value = MagicMock()
        mock_os_cls.return_value = mock_os_instance

        from fastapi import FastAPI
        app = FastAPI()

        async with factory._lifespan(app):
            pass

        container.cleanup.assert_awaited_once()

    @patch("src.infrastructure.web.app_factory.DependencyContainer")
    @patch("src.infrastructure.web.app_factory.AppConfig")
    async def test_lifespan_no_agents_no_teams(self, mock_config_cls, mock_dc_cls):
        """Lifespan sem agentes nem teams — não monta AgentOS."""
        factory = AppFactory()

        mock_config = MagicMock()
        mock_config.mongo_database_name = "db"
        mock_config_cls.load.return_value = mock_config

        controller = MagicMock()
        controller.warm_up_cache = AsyncMock()
        controller.get_agents = AsyncMock(return_value=[])
        controller.get_teams = AsyncMock(return_value=[])
        container = MagicMock()
        container.get_orquestrador_controller.return_value = controller
        container.cleanup = AsyncMock()
        mock_dc_cls.create_async = AsyncMock(return_value=container)

        from fastapi import FastAPI
        app = FastAPI()

        async with factory._lifespan(app):
            pass

        container.cleanup.assert_awaited_once()

    @patch("src.infrastructure.web.app_factory.AgentOS", side_effect=RuntimeError("mount fail"))
    @patch("src.infrastructure.web.app_factory.AGUI")
    @patch("src.infrastructure.web.app_factory.DependencyContainer")
    @patch("src.infrastructure.web.app_factory.AppConfig")
    async def test_lifespan_mount_error_continues(self, mock_config_cls, mock_dc_cls, mock_agui, mock_os_cls):
        """Se AgentOS falhar, lifespan continua sem raise."""
        factory = AppFactory()

        mock_config = MagicMock()
        mock_config.mongo_database_name = "db"
        mock_config_cls.load.return_value = mock_config

        controller = MagicMock()
        agent = MagicMock()
        agent.id = "a1"
        controller.warm_up_cache = AsyncMock()
        controller.get_agents = AsyncMock(return_value=[agent])
        controller.get_teams = AsyncMock(return_value=[])
        container = MagicMock()
        container.get_orquestrador_controller.return_value = controller
        container.cleanup = AsyncMock()
        mock_dc_cls.create_async = AsyncMock(return_value=container)

        mock_agui.return_value = MagicMock()

        from fastapi import FastAPI
        app = FastAPI()

        # Não deve fazer raise
        async with factory._lifespan(app):
            pass

        container.cleanup.assert_awaited_once()

    @patch("src.infrastructure.web.app_factory.DependencyContainer")
    @patch("src.infrastructure.web.app_factory.AppConfig")
    async def test_lifespan_critical_error_raises(self, mock_config_cls, mock_dc_cls):
        """Erro crítico no lifespan deve fazer raise."""
        factory = AppFactory()
        mock_config_cls.load.side_effect = RuntimeError("config fail")

        from fastapi import FastAPI
        app = FastAPI()

        with pytest.raises(RuntimeError, match="config fail"):
            async with factory._lifespan(app):
                pass


# ── create_app module-level ──────────────────────────────────────────


class TestCreateAppModuleLevel:
    def test_create_app_returns_fastapi(self):
        from fastapi import FastAPI
        app = create_app()
        assert isinstance(app, FastAPI)
