import pytest
from unittest.mock import Mock, patch
from src.infrastructure.dependency_injection import DependencyContainer
from src.infrastructure.config.app_config import AppConfig, DatabaseConfig
from src.domain.repositories.agent_config_repository import IAgentConfigRepository
from src.application.services.agent_factory_service import AgentFactoryService
from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase
from src.presentation.controllers.orquestrador_controller import OrquestradorController


class TestDependencyContainer:
    """Testes unitários para DependencyContainer."""
    
    def setup_method(self):
        """Setup executado antes de cada teste."""
        self.db_config = DatabaseConfig(
            connection_string="mongodb://test:27017",
            database_name="test_db"
        )
        self.app_config = AppConfig(
            app_title="Test App",
            database=self.db_config
        )
        self.container = DependencyContainer(self.app_config)
    
    def test_container_initialization(self):
        """Testa se o container é inicializado corretamente."""
        # Assert
        assert self.container._config == self.app_config
        assert self.container._repositories == {}
        assert self.container._services == {}
        assert self.container._use_cases == {}
        assert self.container._controllers == {}
    
    @patch('src.infrastructure.dependency_injection.MongoAgentConfigRepository')
    def test_get_agent_config_repository_creates_singleton(self, mock_repo_class):
        """Testa se o repositório é criado como singleton."""
        # Arrange
        mock_repo = Mock()
        mock_repo_class.return_value = mock_repo
        
        # Act
        repo1 = self.container.get_agent_config_repository()
        repo2 = self.container.get_agent_config_repository()
        
        # Assert
        assert repo1 is repo2  # mesmo objeto (singleton)
        mock_repo_class.assert_called_once_with(
            connection_string="mongodb://test:27017",
            database_name="test_db"
        )
        assert isinstance(repo1, type(mock_repo))
    
    @patch('src.infrastructure.dependency_injection.AgentFactoryService')
    def test_get_agent_factory_service_creates_singleton(self, mock_service_class):
        """Testa se o serviço de factory é criado como singleton."""
        # Arrange
        mock_service = Mock()
        mock_service_class.return_value = mock_service
        
        # Act
        service1 = self.container.get_agent_factory_service()
        service2 = self.container.get_agent_factory_service()
        
        # Assert
        assert service1 is service2  # mesmo objeto (singleton)
        # Verificar se foi chamado com os parâmetros corretos (incluindo tool_repository)
        call_args = mock_service_class.call_args
        assert call_args.kwargs['db_url'] == "mongodb://test:27017"
        assert call_args.kwargs['db_name'] == "test_db"
        assert 'tool_repository' in call_args.kwargs
        mock_service_class.assert_called_once()
    
    @patch('src.infrastructure.dependency_injection.GetActiveAgentsUseCase')
    def test_get_active_agents_use_case_creates_singleton(self, mock_use_case_class):
        """Testa se o caso de uso é criado como singleton."""
        # Arrange
        mock_use_case = Mock()
        mock_use_case_class.return_value = mock_use_case
        
        # Act
        use_case1 = self.container.get_active_agents_use_case()
        use_case2 = self.container.get_active_agents_use_case()
        
        # Assert
        assert use_case1 is use_case2  # mesmo objeto (singleton)
        mock_use_case_class.assert_called_once()
    
    @patch('src.infrastructure.dependency_injection.OrquestradorController')
    def test_get_orquestrador_controller_creates_singleton(self, mock_controller_class):
        """Testa se o controller é criado como singleton."""
        # Arrange
        mock_controller = Mock()
        mock_controller_class.return_value = mock_controller
        
        # Act
        controller1 = self.container.get_orquestrador_controller()
        controller2 = self.container.get_orquestrador_controller()
        
        # Assert
        assert controller1 is controller2  # mesmo objeto (singleton)
        mock_controller_class.assert_called_once()
    
    def test_dependency_injection_flow(self):
        """Testa o fluxo completo de injeção de dependências."""
        # Act
        controller = self.container.get_orquestrador_controller()
        
        # Assert
        assert controller is not None
        assert 'orquestrador' in self.container._controllers
        assert 'get_active_agents' in self.container._use_cases
        assert 'agent_factory' in self.container._services
        assert 'agent_config' in self.container._repositories
    
    def test_container_returns_correct_types(self):
        """Testa se o container retorna os tipos corretos."""
        # Act & Assert
        repo = self.container.get_agent_config_repository()
        assert isinstance(repo, IAgentConfigRepository)
        
        service = self.container.get_agent_factory_service()
        assert isinstance(service, AgentFactoryService)
        
        use_case = self.container.get_active_agents_use_case()
        assert isinstance(use_case, GetActiveAgentsUseCase)
        
        controller = self.container.get_orquestrador_controller()
        assert isinstance(controller, OrquestradorController)
