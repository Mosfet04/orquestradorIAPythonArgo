import pytest
from unittest.mock import Mock, patch, AsyncMock
from src.infrastructure.dependency_injection import DependencyContainer, HealthService
from src.infrastructure.config.app_config import AppConfig


class TestDependencyContainer:
    """Testes unitários para DependencyContainer."""
    
    def setup_method(self):
        """Setup executado antes de cada teste."""
        self.app_config = AppConfig(
            mongo_connection_string="mongodb://test:27017",
            mongo_database_name="test_db",
            app_title="Test App",
            app_host="localhost",
            app_port=8080,
            log_level="DEBUG",
            ollama_base_url="http://localhost:11434",
            openai_api_key="test-key"
        )
        self.container = DependencyContainer(self.app_config)
    
    def test_container_initialization(self):
        """Testa se o container é inicializado corretamente."""
        # Assert
        assert self.container.config == self.app_config
        assert self.container._mongo_client is None
        assert self.container._agent_config_repository is None
        assert self.container._tool_repository is None
        assert self.container._agent_factory_service is None
        assert self.container._get_active_agents_use_case is None
        assert self.container._orquestrador_controller is None
        assert self.container._health_service is None

    @pytest.mark.asyncio
    @patch('src.infrastructure.dependency_injection.AsyncIOMotorClient')
    async def test_create_async_initializes_container(self, mock_motor_client):
        """Testa se o factory assíncrono inicializa o container corretamente."""
        # Arrange
        mock_client = AsyncMock()
        mock_motor_client.return_value = mock_client
        
        # Act
        container = await DependencyContainer.create_async(self.app_config)
        
        # Assert
        assert container.config == self.app_config
        assert container._mongo_client == mock_client
        mock_motor_client.assert_called_once_with(
            "mongodb://test:27017",
            maxPoolSize=20,
            minPoolSize=5,
            connectTimeoutMS=5000,
            serverSelectionTimeoutMS=5000
        )

    @pytest.mark.asyncio
    @patch('src.infrastructure.dependency_injection.MongoAgentConfigRepository')
    async def test_get_agent_config_repository_async_creates_singleton(self, mock_repo_class):
        """Testa se o repositório é criado como singleton assincronamente."""
        # Arrange
        mock_repo = AsyncMock()
        mock_repo_class.return_value = mock_repo
        
        # Act
        repo1 = await self.container._get_agent_config_repository_async()
        repo2 = await self.container._get_agent_config_repository_async()
        
        # Assert
        assert repo1 is repo2  # mesmo objeto (singleton)
        mock_repo_class.assert_called_once_with(
            connection_string="mongodb://test:27017",
            database_name="test_db"
        )

    @pytest.mark.asyncio
    @patch('src.infrastructure.dependency_injection.MongoToolRepository')
    async def test_get_tool_repository_async_creates_singleton(self, mock_repo_class):
        """Testa se o tool repository é criado como singleton assincronamente."""
        # Arrange
        mock_repo = AsyncMock()
        mock_repo_class.return_value = mock_repo
        
        # Act
        repo1 = await self.container._get_tool_repository_async()
        repo2 = await self.container._get_tool_repository_async()
        
        # Assert
        assert repo1 is repo2  # mesmo objeto (singleton)
        mock_repo_class.assert_called_once_with(
            connection_string="mongodb://test:27017",
            database_name="test_db"
        )

    @pytest.mark.asyncio
    @patch('src.infrastructure.dependency_injection.AgentFactoryService')
    async def test_get_agent_factory_service_async_creates_singleton(self, mock_service_class):
        """Testa se o service é criado como singleton assincronamente."""
        # Arrange
        mock_service = AsyncMock()
        mock_service_class.return_value = mock_service
        
        with patch.object(self.container, '_get_tool_repository_async', new_callable=AsyncMock) as mock_tool_repo:
            mock_tool_repo.return_value = Mock()
            
            # Act
            service1 = await self.container._get_agent_factory_service_async()
            service2 = await self.container._get_agent_factory_service_async()
            
            # Assert
            assert service1 is service2  # mesmo objeto (singleton)
            mock_service_class.assert_called_once()

    @pytest.mark.asyncio
    @patch('src.infrastructure.dependency_injection.GetActiveAgentsUseCase')
    async def test_get_active_agents_use_case_async_creates_singleton(self, mock_use_case_class):
        """Testa se o use case é criado como singleton assincronamente."""
        # Arrange
        mock_use_case = AsyncMock()
        mock_use_case_class.return_value = mock_use_case
        
        with patch.object(self.container, '_get_agent_factory_service_async', new_callable=AsyncMock) as mock_agent_factory, \
             patch.object(self.container, '_get_agent_config_repository_async', new_callable=AsyncMock) as mock_agent_repo:
            
            mock_agent_factory.return_value = Mock()
            mock_agent_repo.return_value = Mock()
            
            # Act
            use_case1 = await self.container._get_active_agents_use_case_async()
            use_case2 = await self.container._get_active_agents_use_case_async()
            
            # Assert
            assert use_case1 is use_case2  # mesmo objeto (singleton)
            mock_use_case_class.assert_called_once()

    @pytest.mark.asyncio
    @patch('src.infrastructure.dependency_injection.OrquestradorController')
    async def test_get_orquestrador_controller_async_creates_singleton(self, mock_controller_class):
        """Testa se o controller é criado como singleton assincronamente."""
        # Arrange
        mock_controller = AsyncMock()
        mock_controller_class.return_value = mock_controller
        
        with patch.object(self.container, '_get_active_agents_use_case_async', new_callable=AsyncMock) as mock_use_case:
            mock_use_case.return_value = Mock()
            
            # Act
            controller1 = await self.container.get_orquestrador_controller_async()
            controller2 = await self.container.get_orquestrador_controller_async()
            
            # Assert
            assert controller1 is controller2  # mesmo objeto (singleton)
            mock_controller_class.assert_called_once()

    @pytest.mark.asyncio
    async def test_cleanup_closes_mongo_client(self):
        """Testa se o cleanup fecha o cliente MongoDB."""
        # Arrange
        mock_client = AsyncMock()
        self.container._mongo_client = mock_client
        
        # Act
        await self.container.cleanup()
        
        # Assert
        mock_client.close.assert_called_once()

    def test_health_service_property(self):
        """Testa a propriedade health_service."""
        # Arrange
        mock_health_service = Mock()
        self.container._health_service = mock_health_service
        
        # Act
        result = self.container.health_service
        
        # Assert
        assert result == mock_health_service


class TestHealthService:
    """Testes unitários para HealthService."""
    
    def setup_method(self):
        """Setup executado antes de cada teste."""
        self.mock_mongo_client = AsyncMock()
        self.health_service = HealthService(self.mock_mongo_client)

    @pytest.mark.asyncio
    async def test_check_async_healthy_response(self):
        """Testa resposta saudável do health check."""
        # Arrange
        self.mock_mongo_client.admin.command = AsyncMock()
        
        with patch('psutil.virtual_memory') as mock_memory:
            mock_memory.return_value.percent = 50.0
            mock_memory.return_value.available = 8 * (1024**3)  # 8GB
            
            # Act
            result = await self.health_service.check_async()
            
            # Assert
            assert result["status"] == "healthy"
            assert "mongodb" in result["checks"]
            assert "memory" in result["checks"]
            assert result["checks"]["mongodb"]["status"] == "healthy"
            assert result["checks"]["memory"]["status"] == "healthy"
            assert "response_time_ms" in result

    @pytest.mark.asyncio
    async def test_check_async_unhealthy_mongodb(self):
        """Testa resposta não saudável quando MongoDB falha."""
        # Arrange
        self.mock_mongo_client.admin.command = AsyncMock(side_effect=Exception("Connection failed"))
        
        with patch('psutil.virtual_memory') as mock_memory:
            mock_memory.return_value.percent = 50.0
            mock_memory.return_value.available = 8 * (1024**3)
            
            # Act
            result = await self.health_service.check_async()
            
            # Assert
            assert result["status"] == "unhealthy"
            assert result["checks"]["mongodb"]["status"] == "unhealthy"
            assert "Connection failed" in result["checks"]["mongodb"]["error"]
