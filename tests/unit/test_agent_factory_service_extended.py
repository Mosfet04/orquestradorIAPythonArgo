import pytest
from unittest.mock import Mock, patch
from src.application.services.agent_factory_service import AgentFactoryService
from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.rag_config import RagConfig


class TestAgentFactoryService:
    """Testes unitários para AgentFactoryService com foco em aumentar cobertura."""
    
    def setup_method(self):
        """Setup executado antes de cada teste."""
        self.mock_tool_repository = Mock()
        self.service = AgentFactoryService(
            db_url="mongodb://test:27017",
            db_name="test_db",
            tool_repository=self.mock_tool_repository
        )
    
    def test_agent_factory_service_initialization(self):
        """Testa inicialização do AgentFactoryService."""
        # Assert
        assert self.service._db_url == "mongodb://test:27017"
        assert self.service._db_name == "test_db"
        assert self.service._tool_repository == self.mock_tool_repository
        assert self.service._http_tool_factory is not None
        assert self.service._model_factory is not None
        assert self.service._embedder_model_factory is not None
        assert self.service._model_cache_service is not None
    
    def test_agent_factory_service_initialization_with_defaults(self):
        """Testa inicialização com valores padrão."""
        # Act
        service = AgentFactoryService()
        
        # Assert
        assert service._db_url == "mongodb://localhost:62659/?directConnection=true"
        assert service._db_name == "agno"
        assert service._tool_repository is None
    
    @pytest.mark.asyncio
    async def test_create_agent_async(self):
        """Testa criação assíncrona de agente."""
        # Arrange
        config = AgentConfig(
            id="test_agent",
            nome="Test Agent",
            descricao="Test description",
            prompt="Test prompt",
            factoryIaModel="ollama",
            model="llama2"
        )
        
        mock_agent = Mock()
        
        with patch.object(self.service, 'create_agent', return_value=mock_agent) as mock_create:
            # Act
            result = await self.service.create_agent_async(config)
            
            # Assert
            assert result == mock_agent
            mock_create.assert_called_once_with(config)
    
    @patch('src.application.services.agent_factory_service.Agent')
    @patch('src.application.services.agent_factory_service.MongoDbStorage')
    @patch('src.application.services.agent_factory_service.Memory')
    @patch('src.application.services.agent_factory_service.MongoMemoryDb')
    def test_create_agent_success(self, mock_memory_db, mock_memory, mock_storage, mock_agent):
        """Testa criação bem-sucedida de agente."""
        # Arrange
        config = AgentConfig(
            id="test_agent",
            nome="Test Agent",
            descricao="Test description",
            prompt="Test prompt",
            factoryIaModel="ollama",
            model="llama2"
        )
        
        mock_model = Mock()
        mock_agent_instance = Mock()
        mock_agent.return_value = mock_agent_instance
        
        # Configurar mocks
        with patch.object(self.service._model_factory, 'validate_model_config') as mock_validate:
            mock_validate.return_value = {"valid": True, "errors": []}
            
            with patch.object(self.service, '_get_or_create_model_cached') as mock_get_model:
                mock_get_model.return_value = mock_model
                
                with patch.object(self.service, '_create_memory_db'), \
                     patch.object(self.service, '_create_memory'), \
                     patch.object(self.service, '_create_storage'):
                            with patch.object(self.service, '_create_tools') as mock_create_tools:
                                mock_create_tools.return_value = []
                                
                                # Act
                                result = self.service.create_agent(config)
                                
                                # Assert
                                assert result == mock_agent_instance
                                mock_validate.assert_called_once()
                                mock_get_model.assert_called_once()
    
    def test_create_agent_with_invalid_model_config(self):
        """Testa criação de agente com configuração de modelo inválida."""
        # Arrange
        config = AgentConfig(
            id="test_agent",
            nome="Test Agent",
            descricao="Test description",
            prompt="Test prompt",
            factoryIaModel="invalid_factory",
            model="invalid_model"
        )
        
        with patch.object(self.service._model_factory, 'validate_model_config') as mock_validate:
            mock_validate.return_value = {"valid": False, "errors": ["Modelo inválido"]}
            
            # Act & Assert
            with pytest.raises(ValueError, match="Configuração de modelo inválida"):
                self.service.create_agent(config)
    
    @patch('src.application.services.agent_factory_service.Agent')
    def test_create_agent_with_tools(self, mock_agent):
        """Testa criação de agente com ferramentas."""
        # Arrange
        config = AgentConfig(
            id="test_agent",
            nome="Test Agent",
            descricao="Test description",
            prompt="Test prompt",
            factoryIaModel="ollama",
            model="llama2",
            tools_ids=["tool1", "tool2"]
        )
        
        mock_tools = [Mock(), Mock()]
        mock_agent_instance = Mock()
        mock_agent.return_value = mock_agent_instance
        
        with patch.object(self.service._model_factory, 'validate_model_config') as mock_validate:
            mock_validate.return_value = {"valid": True, "errors": []}
            
            with patch.object(self.service, '_get_or_create_model_cached'):
                with patch.object(self.service, '_create_memory_db'):
                    with patch.object(self.service, '_create_memory'):
                        with patch.object(self.service, '_create_storage'):
                            with patch.object(self.service, '_create_tools') as mock_create_tools:
                                mock_create_tools.return_value = mock_tools
                                
                                # Act
                                self.service.create_agent(config)
                                
                                # Assert
                                mock_create_tools.assert_called_once_with(["tool1", "tool2"])
    
    @patch('src.application.services.agent_factory_service.Agent')
    def test_create_agent_with_rag_config(self, mock_agent):
        """Testa criação de agente com configuração RAG."""
        # Arrange
        rag_config = RagConfig(
            active=True,
            model="test_model",
            doc_name="test_document"
        )
        
        config = AgentConfig(
            id="test_agent",
            nome="Test Agent",
            descricao="Test description",
            prompt="Test prompt",
            factoryIaModel="ollama",
            model="llama2",
            rag_config=rag_config
        )
        
        mock_knowledge_base = Mock()
        mock_agent_instance = Mock()
        mock_agent.return_value = mock_agent_instance
        
        with patch.object(self.service._model_factory, 'validate_model_config') as mock_validate:
            mock_validate.return_value = {"valid": True, "errors": []}
            
            with patch.object(self.service, '_get_or_create_model_cached'):
                with patch.object(self.service, '_create_memory_db'):
                    with patch.object(self.service, '_create_memory'):
                        with patch.object(self.service, '_create_storage'):
                            with patch.object(self.service, '_create_tools'):
                                with patch.object(self.service, '_create_rag') as mock_create_rag:
                                    mock_create_rag.return_value = mock_knowledge_base
                                    
                                    # Act
                                    self.service.create_agent(config)
                                    
                                    # Assert
                                    mock_create_rag.assert_called_once_with(config)
    
    @patch('src.application.services.agent_factory_service.Agent')
    def test_create_agent_with_rag_error(self, mock_agent):
        """Testa criação de agente com erro na criação do RAG."""
        # Arrange
        rag_config = RagConfig(
            active=True,
            model="test_model",
            doc_name="test_document"
        )
        
        config = AgentConfig(
            id="test_agent",
            nome="Test Agent",
            descricao="Test description",
            prompt="Test prompt",
            factoryIaModel="ollama",
            model="llama2",
            rag_config=rag_config
        )
        
        mock_agent_instance = Mock()
        mock_agent.return_value = mock_agent_instance
        
        with patch.object(self.service._model_factory, 'validate_model_config') as mock_validate:
            mock_validate.return_value = {"valid": True, "errors": []}
            
            with patch.object(self.service, '_get_or_create_model_cached'):
                with patch.object(self.service, '_create_memory_db'):
                    with patch.object(self.service, '_create_memory'):
                        with patch.object(self.service, '_create_storage'):
                            with patch.object(self.service, '_create_tools'):
                                with patch.object(self.service, '_create_rag') as mock_create_rag:
                                    mock_create_rag.side_effect = Exception("RAG creation failed")
                                    
                                    # Act (não deve falhar, apenas fazer log do erro)
                                    result = self.service.create_agent(config)
                                    
                                    # Assert
                                    assert result == mock_agent_instance
    
    def test_create_agent_with_exception(self):
        """Testa criação de agente com exceção."""
        # Arrange
        config = AgentConfig(
            id="test_agent",
            nome="Test Agent",
            descricao="Test description",
            prompt="Test prompt",
            factoryIaModel="ollama",
            model="llama2"
        )
        
        with patch.object(self.service._model_factory, 'validate_model_config') as mock_validate:
            mock_validate.side_effect = Exception("Validation error")
            
            # Act & Assert
            with pytest.raises(Exception, match="Validation error"):
                self.service.create_agent(config)
    
    def test_get_or_create_model_cached_method_exists(self):
        """Testa que o método _get_or_create_model_cached existe e pode ser chamado."""
        # Arrange
        factory_model = "ollama"
        model_id = "llama2"

        # Act & Assert
        # Apenas verificar que o método existe e pode ser chamado
        result = self.service._get_or_create_model_cached(factory_model, model_id)
        assert result is not None
    
    def test_create_memory_db_method_exists(self):
        """Testa que o método _create_memory_db existe e pode ser chamado."""
        # Act & Assert
        # Apenas verificar que o método existe e pode ser chamado
        result = self.service._create_memory_db()
        assert result is not None
    
    def test_create_memory_method_exists(self):
        """Testa que o método _create_memory existe e pode ser chamado."""
        # Arrange
        mock_memory_db = Mock()
        mock_model = Mock()
        
        # Act & Assert
        # Apenas verificar que o método existe e pode ser chamado
        result = self.service._create_memory(mock_memory_db, mock_model)
        assert result is not None
    
    def test_create_storage_method_exists(self):
        """Testa que o método _create_storage existe e pode ser chamado."""
        # Act & Assert
        # Apenas verificar que o método existe e pode ser chamado
        result = self.service._create_storage()
        assert result is not None
    
    def test_create_tools_with_repository(self):
        """Testa criação de ferramentas com repositório."""
        # Arrange
        tool_ids = ["tool1", "tool2"]
        mock_tools = [Mock(), Mock()]
        mock_agno_tools = [Mock(), Mock()]
        
        self.mock_tool_repository.get_tools_by_ids.return_value = mock_tools
        
        with patch.object(self.service._http_tool_factory, 'create_tools_from_configs') as mock_create:
            mock_create.return_value = mock_agno_tools
            
            # Act
            result = self.service._create_tools(tool_ids)
            
            # Assert
            assert result == mock_agno_tools
            self.mock_tool_repository.get_tools_by_ids.assert_called_once_with(tool_ids)
            mock_create.assert_called_once_with(mock_tools)
    
    def test_create_tools_without_repository(self):
        """Testa criação de ferramentas sem repositório."""
        # Arrange
        service_without_repo = AgentFactoryService()
        tool_ids = ["tool1", "tool2"]
        
        # Act
        result = service_without_repo._create_tools(tool_ids)
        
        # Assert
        assert result == []
    
    def test_create_tools_with_repository_exception(self):
        """Testa criação de ferramentas com exceção no repositório."""
        # Arrange
        tool_ids = ["tool1", "tool2"]
        self.mock_tool_repository.get_tools_by_ids.side_effect = Exception("Database error")
        
        # Act
        result = self.service._create_tools(tool_ids)
        
        # Assert
        assert result == []
