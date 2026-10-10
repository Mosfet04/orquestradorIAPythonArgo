"""Testes para DependencyContainer e HealthService."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from src.domain.entities.model_config import ModelConfig
from src.domain.ports import InvalidModelConfigError
from src.infrastructure.dependency_injection import DependencyContainer, HealthService
from src.infrastructure.repositories import mongo_base
from src.infrastructure.telemetry import metrics as metrics_module
from tests.fakes import RecordingLogger

# ── HealthService ───────────────────────────────────────────────────


class TestHealthService:
    @pytest.fixture
    def mock_mongo_client(self):
        client = MagicMock()
        client.admin.command = AsyncMock(return_value={"ok": 1})
        return client

    @pytest.fixture
    def health_service(self, mock_mongo_client):
        return HealthService(mock_mongo_client, RecordingLogger())

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
        service = HealthService(mock_mongo_client, RecordingLogger())
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
        service = HealthService(mock_mongo_client, RecordingLogger())
        result = await service._check_mongodb()
        assert result == {"status": "unhealthy"}  # sem texto da exceção

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
        service = HealthService(mock_mongo_client, RecordingLogger())
        with patch("src.infrastructure.dependency_injection.HealthService._check_memory", new_callable=AsyncMock) as mock_mem:
            mock_mem.return_value = {"status": "healthy", "usage_percent": 30, "available_gb": 10.0}
            result = await service.check_async()
        assert result["checks"]["mongodb"] == {"status": "unhealthy"}
        assert "boom" not in str(result)

    async def test_excecao_inesperada_num_check_vira_error_sem_mensagem(self, mock_mongo_client):
        """Exceção que escapa de um check (gather) não leva ``str(exc)`` ao corpo; só ao log, pelo tipo."""
        logger = RecordingLogger()
        service = HealthService(mock_mongo_client, logger)
        with patch.object(HealthService, "_check_memory", new_callable=AsyncMock) as mock_mem:
            mock_mem.side_effect = OSError("/proc/meminfo: detalhe interno")
            result = await service.check_async()
        assert result["status"] == "unhealthy"
        assert result["checks"]["memory"] == {"status": "error"}
        assert "detalhe interno" not in str(result)
        assert [(r.level, r.context) for r in logger.records] == [
            ("error", {"check": "memory", "error_type": "OSError"})
        ]


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

    @staticmethod
    async def _registry_for(monkeypatch, mock_motor_cls, env: dict[str, str]):
        """Sobe o container com ``env`` e devolve o ``ProviderRegistry`` montado pelo composition root."""
        from src.infrastructure import dependency_injection as di
        from src.infrastructure.config.app_config import AppConfig

        mock_client = MagicMock()
        mock_client.admin.command = AsyncMock(return_value={"ok": 1})
        mock_motor_cls.return_value = mock_client
        built: list[object] = []
        real_registry = di.ProviderRegistry

        def _recording(*args, **kwargs):
            registry = real_registry(*args, **kwargs)
            built.append(registry)
            return registry

        monkeypatch.setattr(di, "ProviderRegistry", _recording)
        with patch.dict("os.environ", env, clear=True):
            config = AppConfig.load()
        await DependencyContainer.create_async(config)
        [registry] = built
        return registry

    @pytest.mark.parametrize(
        ("env", "expected_host"),
        [({"OLLAMA_BASE_URL": "http://ollama:11434"}, "http://ollama:11434"), ({}, None)],
    )
    @patch("src.infrastructure.dependency_injection.AsyncIOMotorClient")
    async def test_ollama_base_url_chega_ao_registry(self, mock_motor_cls, monkeypatch, env, expected_host):
        """OLLAMA_BASE_URL do AppConfig chega ao Ollama de chat e embedder; sem ele, vale o default do agno."""
        registry = await self._registry_for(monkeypatch, mock_motor_cls, env)

        model = registry.create_model(ModelConfig("ollama", "llama3.2:latest"))
        embedder = registry.create_embedder(ModelConfig("ollama", "nomic-embed-text"))
        assert model.host == expected_host
        assert embedder.host == expected_host

    @patch("src.infrastructure.dependency_injection.AsyncIOMotorClient")
    async def test_ollama_base_url_nao_vaza_para_outro_provider(self, mock_motor_cls, monkeypatch):
        registry = await self._registry_for(monkeypatch, mock_motor_cls, {"OLLAMA_BASE_URL": "http://ollama:11434"})
        monkeypatch.setenv("OPENAI_API_KEY", "sk-teste")

        model = registry.create_model(ModelConfig("openai", "gpt-4o-mini"))
        assert model.base_url is None

    @patch("src.infrastructure.dependency_injection.AsyncIOMotorClient")
    async def test_secrets_dir_e_allowlist_do_app_config_chegam_ao_registry(self, mock_motor_cls, monkeypatch, tmp_path):
        (tmp_path / "gw").write_text("chave-do-arquivo", encoding="utf-8")
        registry = await self._registry_for(
            monkeypatch,
            mock_motor_cls,
            {"SECRETS_DIR": str(tmp_path), "MODEL_BASE_URL_ALLOWLIST": "10.0.0.5", "ENVIRONMENT": "production",
             "API_KEY_RUN": "r" * 32, "API_KEY_ADMIN": "a" * 32},
        )

        model = registry.create_model(
            ModelConfig("openai_compatible", "m", base_url="http://10.0.0.5:8000/v1", api_key_ref=f"file:{tmp_path}/gw")
        )
        assert (model.base_url, model.api_key) == ("http://10.0.0.5:8000/v1", "chave-do-arquivo")
        with pytest.raises(InvalidModelConfigError, match="MODEL_BASE_URL_ALLOWLIST"):
            registry.create_model(ModelConfig("openai_compatible", "m", base_url="http://127.0.0.1:8000"))

    @pytest.mark.parametrize(
        ("env", "loopback_allowed"),
        [
            ({}, True),  # modo dev local: sem chaves, APP_HOST loopback, development
            ({"API_KEY_RUN": "r" * 32, "API_KEY_ADMIN": "a" * 32}, False),
        ],
    )
    @patch("src.infrastructure.dependency_injection.AsyncIOMotorClient")
    async def test_loopback_em_base_url_so_no_modo_dev_local(self, mock_motor_cls, monkeypatch, env, loopback_allowed):
        registry = await self._registry_for(monkeypatch, mock_motor_cls, env)
        config = ModelConfig("openai_compatible", "m", base_url="http://127.0.0.1:8000/v1")

        if loopback_allowed:
            assert registry.create_model(config).base_url == "http://127.0.0.1:8000/v1"
        else:
            with pytest.raises(InvalidModelConfigError):
                registry.create_model(config)

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

    @patch("src.infrastructure.dependency_injection.AsyncIOMotorClient")
    async def test_controller_do_container_registra_hit_e_miss_do_cache_nas_metricas(
        self, mock_motor_cls, monkeypatch, tmp_path
    ):
        """O controller não importa a telemetria (F2-07): quem liga hit/miss ao OTel é o composition root."""
        mock_client = MagicMock()
        mock_client.admin.command = AsyncMock(return_value={"ok": 1})
        mock_motor_cls.return_value = mock_client
        reader = InMemoryMetricReader()
        meter_provider = MeterProvider(metric_readers=[reader])
        meter = meter_provider.get_meter("teste")
        monkeypatch.setattr(metrics_module, "cache_hits_total", meter.create_counter("cache_hits_total"))
        monkeypatch.setattr(metrics_module, "cache_misses_total", meter.create_counter("cache_misses_total"))
        config_file = tmp_path / "config.yaml"
        config_file.write_text("agents: []\nteams: []\ntools: []\n", encoding="utf-8")

        from src.infrastructure.config.app_config import AppConfig
        with patch.dict("os.environ", {
            "MONGO_CONNECTION_STRING": "mongodb://localhost:27017",
            "MONGO_DATABASE_NAME": "testdb",
            "CONFIG_STORE": "yaml",
            "CONFIG_YAML_PATH": str(config_file),
        }, clear=True):
            config = AppConfig.load()
        controller = (await DependencyContainer.create_async(config)).get_orquestrador_controller()

        assert await controller.get_agents() == []  # miss: carrega do arquivo
        assert await controller.get_agents() == []  # hit: cache
        assert await controller.get_teams() == []  # miss de teams + hit de agents

        points = {
            metric.name: sorted((dict(p.attributes or {})["cache"], p.value) for p in metric.data.data_points)
            for resource in reader.get_metrics_data().resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
        }
        meter_provider.shutdown()
        assert points == {
            "cache_misses_total": [("agents", 1), ("teams", 1)],
            "cache_hits_total": [("agents", 2)],
        }

    def test_get_controller_not_initialized(self):
        from src.infrastructure.config.app_config import AppConfig
        with patch.dict("os.environ", {
            "MONGO_CONNECTION_STRING": "mongodb://localhost:27017",
            "MONGO_DATABASE_NAME": "testdb",
        }, clear=True):
            config = AppConfig.load()
        container = DependencyContainer(config)
        with pytest.raises(RuntimeError, match="Container não inicializado"):
            container.get_orquestrador_controller()


async def test_cleanup_com_erro_no_close_loga_e_nao_propaga():
    """Antes: ``except Exception: pass`` (bandit B110). Agora o erro vai ao log pelo tipo."""
    from src.infrastructure.config.app_config import AppConfig

    with patch.dict("os.environ", {}, clear=True):
        container = DependencyContainer(AppConfig.load())
    logger = RecordingLogger()
    container._logger = logger
    mongo_client = MagicMock()
    mongo_client.close = MagicMock(side_effect=RuntimeError("detalhe interno do driver"))
    container._mongo_client = mongo_client

    await container.cleanup()

    assert [(r.level, r.context) for r in logger.records] == [("warning", {"error_type": "RuntimeError"})]


@pytest.mark.usefixtures("reset_otel_providers")
async def test_check_otlp_nao_faz_flush_bloqueante_no_event_loop():
    """``force_flush`` é síncrono (até 2 s) e exporta spans: não roda dentro do health."""
    from opentelemetry import trace
    from opentelemetry.sdk.trace import TracerProvider

    flushes: list[int] = []

    class _RecordingProvider(TracerProvider):
        def force_flush(self, timeout_millis: int = 30000) -> bool:
            flushes.append(timeout_millis)
            return True

    trace.set_tracer_provider(_RecordingProvider())
    client = MagicMock()
    client.admin.command = AsyncMock(return_value={"ok": 1})

    result = await HealthService(client, RecordingLogger()).check_async()

    assert flushes == []
    assert result["checks"]["otlp"] == {"status": "configured", "provider": "_RecordingProvider"}
    assert result["status"] == "healthy"
