"""Testes estendidos para AppFactory — cobertura de middleware, lifespan e helpers."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from httpx import ASGITransport, AsyncClient

from src.infrastructure.web.app_factory import (
    AppFactory,
    create_app,
)
from tests.fakes.web import mount_agent_os

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
        container.health_service.check_async = AsyncMock(return_value={"status": "healthy", "db": "connected"})
        container.cleanup = AsyncMock()
        factory._container = container
        return factory

    async def test_health_with_health_service(self, factory_with_container):
        """health_check deve chamar health_service quando disponível."""
        app = factory_with_container.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:7777") as client:
            resp = await client.get("/admin/health")
            assert resp.status_code == 200
            assert resp.json()["status"] == "healthy"

    async def test_health_unhealthy_responde_503(self, factory_with_container):
        factory_with_container._container.health_service.check_async = AsyncMock(
            return_value={"status": "unhealthy", "checks": {"mongodb": {"status": "unhealthy"}}}
        )
        app = factory_with_container.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:7777") as client:
            resp = await client.get("/admin/health")
        assert resp.status_code == 503
        assert resp.json() == {"status": "unhealthy", "checks": {"mongodb": {"status": "unhealthy"}}}

    async def test_cache_metrics_with_container(self, factory_with_container):
        app = factory_with_container.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:7777") as client:
            resp = await client.get("/metrics/cache")
            assert resp.status_code == 200
            assert resp.json()["agents"]["status"] == "active"

    async def test_refresh_cache_with_container(self, factory_with_container):
        app = factory_with_container.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:7777") as client:
            resp = await client.post("/admin/refresh-cache")
            assert resp.status_code == 200
            assert resp.json()["status"] == "cache_refreshed"

    async def test_health_no_container(self):
        """Sem container, health_check retorna healthy padrão."""
        factory = AppFactory()
        app = factory.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:7777") as client:
            resp = await client.get("/admin/health")
            assert resp.json()["status"] == "healthy"

    async def test_cache_metrics_no_container(self):
        """Sem container, retorna no_cache."""
        factory = AppFactory()
        app = factory.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:7777") as client:
            resp = await client.get("/metrics/cache")
            assert resp.json()["status"] == "no_cache"

    async def test_refresh_no_container(self):
        """Sem container, retorna no_cache."""
        factory = AppFactory()
        app = factory.create_app()
        transport = ASGITransport(app=app)
        async with AsyncClient(transport=transport, base_url="http://127.0.0.1:7777") as client:
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
    def container(self):
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
        return container

    async def test_load_agents(self, container):
        agents = await AppFactory()._load_agents(container)
        assert [a.id for a in agents] == ["a1", "a2"]
        container.get_orquestrador_controller.return_value.warm_up_cache.assert_awaited_once()

    async def test_load_teams(self, container):
        teams = await AppFactory()._load_teams(container)
        assert [t.id for t in teams] == ["t1"]


# ── _mount_runtime ───────────────────────────────────────────────────


class TestMountRuntime:
    @patch("src.infrastructure.runtime.agno.runtime.AgentOS")
    async def test_mount_agent_os_success(self, mock_os_cls):
        factory = AppFactory()
        mock_os_instance = MagicMock()
        mock_os_instance.get_app.return_value = MagicMock()
        mock_os_cls.return_value = mock_os_instance

        app = factory.create_app()  # carrega o AppConfig (origens do CORS)
        agents = [MagicMock(), MagicMock()]
        teams = [MagicMock()]

        mount_agent_os(factory, app, agents, teams)
        mock_os_cls.assert_called_once()
        mock_os_instance.get_app.assert_called_once()

    @patch("src.infrastructure.runtime.agno.runtime.AgentOS")
    async def test_mount_agent_os_empty_teams(self, mock_os_cls):
        factory = AppFactory()
        mock_os_instance = MagicMock()
        mock_os_instance.get_app.return_value = MagicMock()
        mock_os_cls.return_value = mock_os_instance

        app = factory.create_app()
        mount_agent_os(factory, app, [MagicMock()], [])
        # teams=[] → deve enviar None
        call_kwargs = mock_os_cls.call_args[1]
        assert call_kwargs.get("teams") is None

    def test_mount_sem_config_carregado_falha_com_mensagem_clara(self):
        from fastapi import FastAPI

        runtime = MagicMock()
        with pytest.raises(RuntimeError, match="create_app"):
            AppFactory()._mount_runtime(FastAPI(), runtime, [MagicMock()], [])
        runtime.mount.assert_not_called()


# ── _lifespan ────────────────────────────────────────────────────────


def _container_with(agents, teams):
    """Container falso: config, controller com as entidades e runtime espião."""
    config = MagicMock()
    config.mongo_database_name = "db"
    controller = MagicMock()
    controller.warm_up_cache = AsyncMock()
    controller.get_agents = AsyncMock(return_value=agents)
    controller.get_teams = AsyncMock(return_value=teams)
    runtime = MagicMock()
    runtime.start = AsyncMock()
    runtime.close = AsyncMock()
    container = MagicMock()
    container.config = config
    container.get_orquestrador_controller.return_value = controller
    container.get_agent_runtime.return_value = runtime
    container.cleanup = AsyncMock()
    return container


@patch("src.infrastructure.web.app_factory.shutdown_telemetry")
@patch("src.infrastructure.web.app_factory.setup_telemetry")
@patch("src.infrastructure.web.app_factory.DependencyContainer")
class TestLifespan:
    async def test_lifespan_happy_path(self, mock_dc_cls, mock_setup_tel, mock_shutdown_tel):
        """Container -> entidades -> runtime montado e iniciado; shutdown fecha runtime e container uma vez."""
        agent = MagicMock()
        agent.id = "a1"
        container = _container_with([agent], [])
        mock_dc_cls.create_async = AsyncMock(return_value=container)
        factory = AppFactory()
        app = factory.create_app()
        runtime = container.get_agent_runtime.return_value

        async with factory._lifespan(app):
            runtime.mount.assert_called_once()
            assert runtime.mount.call_args.args == (app, [agent], [])
            runtime.start.assert_awaited_once()
            runtime.close.assert_not_awaited()

        runtime.close.assert_awaited_once()
        container.cleanup.assert_awaited_once()
        mock_shutdown_tel.assert_called_once()

    async def test_lifespan_no_agents_no_teams(self, mock_dc_cls, mock_setup_tel, mock_shutdown_tel):
        """Sem agentes nem teams: não monta (start do runtime sem montagem não faz nada)."""
        container = _container_with([], [])
        mock_dc_cls.create_async = AsyncMock(return_value=container)
        factory = AppFactory()
        app = factory.create_app()
        runtime = container.get_agent_runtime.return_value

        async with factory._lifespan(app):
            pass

        runtime.mount.assert_not_called()
        runtime.close.assert_awaited_once()
        container.cleanup.assert_awaited_once()

    async def test_lifespan_mount_error_continues(self, mock_dc_cls, mock_setup_tel, mock_shutdown_tel):
        """Se a montagem falhar, o lifespan continua sem raise e o shutdown fecha tudo uma vez."""
        agent = MagicMock()
        agent.id = "a1"
        container = _container_with([agent], [])
        runtime = container.get_agent_runtime.return_value
        runtime.mount.side_effect = RuntimeError("mount fail")
        mock_dc_cls.create_async = AsyncMock(return_value=container)
        factory = AppFactory()
        app = factory.create_app()

        async with factory._lifespan(app):
            pass

        runtime.close.assert_awaited_once()
        container.cleanup.assert_awaited_once()

    async def test_falha_ao_fechar_o_runtime_ainda_fecha_telemetria_e_container(
        self, mock_dc_cls, mock_setup_tel, mock_shutdown_tel
    ):
        container = _container_with([MagicMock(id="a1")], [])
        runtime = container.get_agent_runtime.return_value
        runtime.close.side_effect = RuntimeError("close fail")
        mock_dc_cls.create_async = AsyncMock(return_value=container)
        factory = AppFactory()
        app = factory.create_app()

        with pytest.raises(RuntimeError, match="close fail"):
            async with factory._lifespan(app):
                pass

        mock_shutdown_tel.assert_called_once()
        container.cleanup.assert_awaited_once()

    async def test_shutdown_faz_flush_da_telemetria_antes_de_fechar_o_runtime(
        self, mock_dc_cls, mock_setup_tel, mock_shutdown_tel
    ):
        container = _container_with([MagicMock(id="a1")], [])
        runtime = container.get_agent_runtime.return_value
        order: list[str] = []
        mock_shutdown_tel.side_effect = lambda: order.append("telemetry")
        runtime.close.side_effect = lambda: order.append("runtime")
        container.cleanup.side_effect = lambda: order.append("container")
        mock_dc_cls.create_async = AsyncMock(return_value=container)
        factory = AppFactory()
        app = factory.create_app()

        async with factory._lifespan(app):
            pass

        assert order == ["telemetry", "runtime", "container"]

    async def test_segundo_lifespan_no_mesmo_factory_e_recusado_sem_tocar_no_container(
        self, mock_dc_cls, mock_setup_tel, mock_shutdown_tel
    ):
        container = _container_with([MagicMock(id="a1")], [])
        mock_dc_cls.create_async = AsyncMock(return_value=container)
        factory = AppFactory()
        app = factory.create_app()
        async with factory._lifespan(app):
            pass

        with pytest.raises(RuntimeError, match="Lifespan já executado"):
            async with factory._lifespan(app):
                pass  # pragma: no cover - recusado antes de subir

        mock_dc_cls.create_async.assert_awaited_once()
        container.get_agent_runtime.return_value.close.assert_awaited_once()
        container.cleanup.assert_awaited_once()

    async def test_falha_no_start_do_runtime_sobe_e_fecha_runtime_e_container(
        self, mock_dc_cls, mock_setup_tel, mock_shutdown_tel
    ):
        container = _container_with([MagicMock(id="a1")], [])
        runtime = container.get_agent_runtime.return_value
        runtime.start.side_effect = RuntimeError("start fail")
        mock_dc_cls.create_async = AsyncMock(return_value=container)
        factory = AppFactory()
        app = factory.create_app()

        with pytest.raises(RuntimeError, match="start fail"):
            async with factory._lifespan(app):
                pass  # pragma: no cover - o startup não completa

        runtime.close.assert_awaited_once()
        container.cleanup.assert_awaited_once()

    @patch("src.infrastructure.web.app_factory.AppConfig")
    async def test_lifespan_critical_error_raises(self, mock_config_cls, mock_dc_cls, mock_setup_tel, mock_shutdown_tel):
        """Erro crítico no lifespan deve fazer raise."""
        factory = AppFactory()
        mock_config_cls.load.side_effect = RuntimeError("config fail")

        from fastapi import FastAPI
        app = FastAPI()

        with pytest.raises(RuntimeError, match="config fail"):
            async with factory._lifespan(app):
                pass
        mock_dc_cls.create_async.assert_not_called()


# ── create_app module-level ──────────────────────────────────────────


class TestCreateAppModuleLevel:
    def test_create_app_returns_fastapi(self):
        from fastapi import FastAPI
        app = create_app()
        assert isinstance(app, FastAPI)
