import pytest
from unittest.mock import AsyncMock, Mock, patch

from src.infrastructure.web.app_factory import AppFactory
from src.infrastructure.config.app_config import AppConfig


@pytest.mark.asyncio
async def test_app_factory_creates_app_and_mounts(monkeypatch):
    factory = AppFactory()

    # Mock AppConfig.load_async
    monkeypatch.setattr(AppConfig, 'load_async', AsyncMock(return_value=AppConfig(
        mongo_connection_string="mongodb://localhost:62659/?directConnection=true",
        mongo_database_name="agno",
        app_title="t",
        app_host="0.0.0.0",
        app_port=7777,
        log_level="INFO",
        ollama_base_url="http://localhost:11434",
        openai_api_key=None
    )))

    # Mock DependencyContainer.create_async e métodos do controller
    fake_controller = Mock()
    FakeSubApp = Mock()
    FakeSubApp.get_app = Mock(return_value=Mock())
    fake_controller.create_playground_async = AsyncMock(return_value=FakeSubApp)
    fake_controller.create_fastapi_app_async = AsyncMock(return_value=FakeSubApp)
    fake_controller.get_cache_stats = Mock(return_value={"hits": 0})
    fake_controller.refresh_agents_async = AsyncMock()
    fake_controller.warm_up_cache = AsyncMock()

    class FakeContainer:
        def __init__(self):
            self.health_service = Mock()
            self.health_service.check_async = AsyncMock(return_value={"status": "healthy"})
        async def get_orquestrador_controller_async(self):
            return fake_controller
        async def cleanup(self):
            return None

    with patch('src.infrastructure.web.app_factory.DependencyContainer.create_async', AsyncMock(return_value=FakeContainer())):
        app = await factory.create_app_async()
        # Checar rotas montadas por representação textual
        routes_str = "\n".join(str(r) for r in app.routes)
        assert any(seg in routes_str for seg in ["/playground", "/api", "/health"]) 
