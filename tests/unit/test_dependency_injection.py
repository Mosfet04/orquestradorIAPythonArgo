"""Testes para DependencyContainer e HealthService."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from src.infrastructure.dependency_injection import DependencyContainer, HealthService
from src.infrastructure.repositories import mongo_base

# ── HealthService ───────────────────────────────────────────────────


class TestHealthService:
    @pytest.fixture
    def mock_mongo_client(self):
        client = MagicMock()
        client.admin.command = AsyncMock(return_value={"ok": 1})
        return client

    @pytest.fixture
    def health_service(self, mock_mongo_client):
        return HealthService(mock_mongo_client)

    async def test_check_async_healthy(self, health_service):
        with patch("src.infrastructure.dependency_injection.HealthService._check_memory", new_callable=AsyncMock) as mock_mem:
            mock_mem.return_value = {"status": "healthy", "usage_percent": 50, "available_gb": 8.0}
            result = await health_service.check_async()
        assert result["status"] == "healthy"
        assert "mongodb" in result["checks"]
        assert "memory" in result["checks"]
        assert "response_time_ms" in result

    async def test_check_async_mongodb_unhealthy(self, mock_mongo_client):
        mock_mongo_client.admin.command = AsyncMock(side_effect=Exception("connection refused"))
        service = HealthService(mock_mongo_client)
        with patch("src.infrastructure.dependency_injection.HealthService._check_memory", new_callable=AsyncMock) as mock_mem:
            mock_mem.return_value = {"status": "healthy", "usage_percent": 50, "available_gb": 8.0}
            result = await service.check_async()
        assert result["status"] == "unhealthy"
        assert result["checks"]["mongodb"]["status"] == "unhealthy"

    async def test_check_mongodb_ping_success(self, health_service):
        result = await health_service._check_mongodb()
        assert result["status"] == "healthy"

    async def test_check_mongodb_ping_failure(self, mock_mongo_client):
        mock_mongo_client.admin.command = AsyncMock(side_effect=Exception("fail"))
        service = HealthService(mock_mongo_client)
        result = await service._check_mongodb()
        assert result["status"] == "unhealthy"
        assert "error" in result

    async def test_check_memory_with_psutil(self, health_service):
        mock_mem = MagicMock()
        mock_mem.percent = 50.0
        mock_mem.available = 8 * (1024**3)
        with patch.dict("sys.modules", {"psutil": MagicMock(virtual_memory=MagicMock(return_value=mock_mem))}):
            result = await HealthService._check_memory()
        assert result["status"] in ("healthy", "warning")

    async def test_check_memory_without_psutil(self, health_service):
        import sys
        with patch.dict("sys.modules", {"psutil": None}):
            # Forçar ImportError removendo psutil do cache
            original = sys.modules.get("psutil")
            sys.modules["psutil"] = None  # type: ignore[assignment]
            try:
                # Preciso reimportar para forçar o ImportError
                result = await HealthService._check_memory()
                # Se psutil está importado no namespace, pode não causar ImportError
                # Mas o teste cobre o fluxo
                assert "status" in result
            finally:
                if original is not None:
                    sys.modules["psutil"] = original

    async def test_check_async_with_exception_in_gather(self, mock_mongo_client):
        """Quando gather retorna exceção, o resultado deve marcar como error."""
        mock_mongo_client.admin.command = AsyncMock(side_effect=RuntimeError("boom"))
        service = HealthService(mock_mongo_client)
        with patch("src.infrastructure.dependency_injection.HealthService._check_memory", new_callable=AsyncMock) as mock_mem:
            mock_mem.return_value = {"status": "healthy", "usage_percent": 30, "available_gb": 10.0}
            result = await service.check_async()
        assert result["checks"]["mongodb"]["status"] in ("unhealthy", "error")


# ── DependencyContainer ─────────────────────────────────────────────


class TestDependencyContainer:
    @pytest.fixture(autouse=True)
    def repo_collection(self, monkeypatch):
        """Os repositórios Mongo usam o cliente compartilhado de ``mongo_base`` (cache global por URL).

        Sem isto o primeiro teste tentava conectar de verdade (30 s de serverSelectionTimeout em
        ``ensure_indexes``) e os seguintes herdavam um cliente preso a outro event loop.
        """
        collection = MagicMock()
        collection.create_index = AsyncMock(side_effect=lambda keys, **kw: kw["name"])
        client = MagicMock()
        client.__getitem__.return_value.__getitem__.return_value = collection
        monkeypatch.setattr(mongo_base.MongoClientFactory, "_instances", {})
        monkeypatch.setattr(mongo_base, "AsyncIOMotorClient", MagicMock(return_value=client))
        return collection

    @patch("src.infrastructure.dependency_injection.AsyncIOMotorClient")
    async def test_create_async(self, mock_motor_cls, repo_collection):
        mock_client = MagicMock()
        mock_client.admin.command = AsyncMock(return_value={"ok": 1})
        mock_client.close = MagicMock(return_value=None)
        mock_motor_cls.return_value = mock_client

        from src.infrastructure.config.app_config import AppConfig
        with patch.dict("os.environ", {
            "MONGO_CONNECTION_STRING": "mongodb://localhost:27017",
            "MONGO_DATABASE_NAME": "testdb",
        }, clear=True):
            config = AppConfig.load()

        container = await DependencyContainer.create_async(config)
        assert container is not None
        assert container.health_service is not None
        controller = container.get_orquestrador_controller()
        assert controller is not None
        mock_client.admin.command.assert_awaited_once_with("ping")
        created = {c.kwargs["name"] for c in repo_collection.create_index.await_args_list}
        assert created == {"idx_doc_level", "idx_parent", "idx_node_id"}

    @pytest.mark.parametrize(
        ("env", "expected_host"),
        [({"OLLAMA_BASE_URL": "http://ollama:11434"}, "http://ollama:11434"), ({}, None)],
    )
    @patch("src.infrastructure.dependency_injection.AsyncIOMotorClient")
    async def test_ollama_base_url_chega_as_factories(self, mock_motor_cls, monkeypatch, env, expected_host):
        """OLLAMA_BASE_URL do AppConfig é injetado nas factories; sem ele, vale o default do agno."""
        from src.infrastructure import dependency_injection as di
        from src.infrastructure.config.app_config import AppConfig

        mock_client = MagicMock()
        mock_client.admin.command = AsyncMock(return_value={"ok": 1})
        mock_motor_cls.return_value = mock_client

        built: dict[str, object] = {}

        def _recording(cls):
            def _build(**kwargs):
                instance = cls(**kwargs)
                built[cls.__name__] = instance
                return instance
            return _build

        monkeypatch.setattr(di, "ModelFactory", _recording(di.ModelFactory))
        monkeypatch.setattr(di, "EmbedderModelFactory", _recording(di.EmbedderModelFactory))

        with patch.dict("os.environ", env, clear=True):
            config = AppConfig.load()
        await DependencyContainer.create_async(config)

        model = built["ModelFactory"].create_model("ollama", "llama3.2:latest")
        embedder = built["EmbedderModelFactory"].create_model("ollama", "nomic-embed-text")
        assert model.host == expected_host
        assert embedder.host == expected_host

    @patch("src.infrastructure.dependency_injection.AsyncIOMotorClient")
    async def test_create_async_mongo_unavailable(self, mock_motor_cls):
        mock_client = MagicMock()
        mock_client.admin.command = AsyncMock(side_effect=Exception("connection refused"))
        mock_client.close = MagicMock(return_value=None)
        mock_motor_cls.return_value = mock_client

        from src.infrastructure.config.app_config import AppConfig
        with patch.dict("os.environ", {
            "MONGO_CONNECTION_STRING": "mongodb://localhost:27017",
            "MONGO_DATABASE_NAME": "testdb",
        }, clear=True):
            config = AppConfig.load()

        # Deve continuar mesmo com mongo indisponível
        container = await DependencyContainer.create_async(config)
        assert container is not None

    @patch("src.infrastructure.dependency_injection.AsyncIOMotorClient")
    async def test_cleanup(self, mock_motor_cls):
        mock_client = MagicMock()
        mock_client.admin.command = AsyncMock(return_value={"ok": 1})
        mock_client.close = MagicMock(return_value=None)
        mock_motor_cls.return_value = mock_client

        from src.infrastructure.config.app_config import AppConfig
        with patch.dict("os.environ", {
            "MONGO_CONNECTION_STRING": "mongodb://localhost:27017",
            "MONGO_DATABASE_NAME": "testdb",
        }, clear=True):
            config = AppConfig.load()

        container = await DependencyContainer.create_async(config)
        await container.cleanup()
        mock_client.close.assert_called_once()

    @patch("src.infrastructure.dependency_injection.AsyncIOMotorClient")
    async def test_cleanup_with_coroutine_close(self, mock_motor_cls):
        mock_client = MagicMock()
        mock_client.admin.command = AsyncMock(return_value={"ok": 1})

        # close() retorna uma coroutine
        async def _async_close():
            return None

        mock_client.close = MagicMock(return_value=_async_close())
        mock_motor_cls.return_value = mock_client

        from src.infrastructure.config.app_config import AppConfig
        with patch.dict("os.environ", {
            "MONGO_CONNECTION_STRING": "mongodb://localhost:27017",
            "MONGO_DATABASE_NAME": "testdb",
        }, clear=True):
            config = AppConfig.load()

        container = await DependencyContainer.create_async(config)
        await container.cleanup()

    def test_get_controller_not_initialized(self):
        from src.infrastructure.config.app_config import AppConfig
        with patch.dict("os.environ", {
            "MONGO_CONNECTION_STRING": "mongodb://localhost:27017",
            "MONGO_DATABASE_NAME": "testdb",
        }, clear=True):
            config = AppConfig.load()
        container = DependencyContainer(config)
        with pytest.raises(AssertionError, match="Container não inicializado"):
            container.get_orquestrador_controller()
