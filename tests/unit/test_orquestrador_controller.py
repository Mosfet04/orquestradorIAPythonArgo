import pytest
from unittest.mock import Mock, MagicMock
from src.presentation.controllers.orquestrador_controller import OrquestradorController
from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase


class TestOrquestradorController:
    """Testes unitários para OrquestradorController."""
    
    def setup_method(self):
        """Setup executado antes de cada teste."""
        self.mock_use_case = Mock(spec=GetActiveAgentsUseCase)
        self.controller = OrquestradorController(self.mock_use_case)
    
    def test_controller_initialization(self):
        """Testa se o controller é inicializado corretamente."""
        # Assert
        assert self.controller._get_active_agents_use_case == self.mock_use_case
        assert self.controller._agents_cache is None
    
    def test_get_agents_calls_use_case_first_time(self):
        """Testa se get_agents chama o caso de uso na primeira vez."""
        # Arrange
        mock_agents = [Mock(), Mock()]
        self.mock_use_case.execute.return_value = mock_agents
        
        # Act
        agents = self.controller.get_agents()
        
        # Assert
        assert agents == mock_agents
        assert self.controller._agents_cache == mock_agents
        self.mock_use_case.execute.assert_called_once()
    
    def test_get_agents_uses_cache_on_subsequent_calls(self):
        """Testa se get_agents usa cache nas chamadas subsequentes."""
        # Arrange
        mock_agents = [Mock(), Mock()]
        self.mock_use_case.execute.return_value = mock_agents
        
        # Act
        agents1 = self.controller.get_agents()
        agents2 = self.controller.get_agents()
        
        # Assert
        assert agents1 == agents2
        assert agents1 is agents2  # mesma instância (cache)
        self.mock_use_case.execute.assert_called_once()  # só chamado uma vez
    
    def test_refresh_agents_clears_cache(self):
        """Testa se refresh_agents limpa o cache."""
        # Arrange
        mock_agents = [Mock(), Mock()]
        self.mock_use_case.execute.return_value = mock_agents
        self.controller.get_agents()  # popula o cache
        
        # Act
        self.controller.refresh_agents()
        
        # Assert
        assert self.controller._agents_cache is None
    
    def test_refresh_agents_forces_new_call_to_use_case(self):
        """Testa se refresh_agents força nova chamada ao caso de uso."""
        # Arrange
        mock_agents1 = [Mock(), Mock()]
        mock_agents2 = [Mock(), Mock(), Mock()]
        self.mock_use_case.execute.side_effect = [mock_agents1, mock_agents2]
        
        # Act
        agents1 = self.controller.get_agents()
        self.controller.refresh_agents()
        agents2 = self.controller.get_agents()
        
        # Assert
        assert agents1 != agents2
        assert len(agents1) == 2
        assert len(agents2) == 3
        assert self.mock_use_case.execute.call_count == 2
    
    @pytest.fixture
    def mock_playground_class(self):
        """Fixture para mockar a classe Playground."""
        with pytest.Mock('src.presentation.controllers.orquestrador_controller.Playground') as mock:
            yield mock
    
    def test_create_playground_creates_with_agents(self):
        """Testa se create_playground cria playground com agentes."""
        # Arrange
        mock_agents = [Mock(), Mock()]
        self.mock_use_case.execute.return_value = mock_agents
        
        # Act
        playground = self.controller.create_playground()
        
        # Assert
        assert playground is not None
        # Verificar se foi chamado com os agentes corretos
        self.mock_use_case.execute.assert_called_once()
    
    def test_create_fastapi_app_creates_with_agents(self):
        """Testa se create_fastapi_app cria app com agentes."""
        # Arrange
        mock_agents = [Mock(), Mock()]
        self.mock_use_case.execute.return_value = mock_agents
        
        # Act
        fastapi_app = self.controller.create_fastapi_app()
        
        # Assert
        assert fastapi_app is not None
        # Verificar se foi chamado com os agentes corretos
        self.mock_use_case.execute.assert_called_once()
    
    def test_create_playground_uses_cached_agents(self):
        """Testa se create_playground usa agentes em cache."""
        # Arrange
        mock_agents = [Mock(), Mock()]
        self.mock_use_case.execute.return_value = mock_agents
        self.controller.get_agents()  # popula cache
        
        # Act
        playground = self.controller.create_playground()
        
        # Assert
        assert playground is not None
        # Use case deve ser chamado apenas uma vez (para popular cache)
        self.mock_use_case.execute.assert_called_once()
    
    def test_create_fastapi_app_uses_cached_agents(self):
        """Testa se create_fastapi_app usa agentes em cache."""
        # Arrange
        mock_agents = [Mock(), Mock()]
        self.mock_use_case.execute.return_value = mock_agents
        self.controller.get_agents()  # popula cache
        
        # Act
        fastapi_app = self.controller.create_fastapi_app()
        
        # Assert
        assert fastapi_app is not None
        # Use case deve ser chamado apenas uma vez (para popular cache)
        self.mock_use_case.execute.assert_called_once()
    
    def test_multiple_operations_use_same_agents(self):
        """Testa se múltiplas operações usam os mesmos agentes."""
        # Arrange
        mock_agents = [Mock(), Mock()]
        self.mock_use_case.execute.return_value = mock_agents
        
        # Act
        agents = self.controller.get_agents()
        playground = self.controller.create_playground()
        fastapi_app = self.controller.create_fastapi_app()
        
        # Assert
        assert agents is not None
        assert playground is not None
        assert fastapi_app is not None
        # Use case deve ser chamado apenas uma vez
        self.mock_use_case.execute.assert_called_once()
