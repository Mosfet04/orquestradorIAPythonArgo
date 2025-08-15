import pytest
from unittest.mock import Mock, MagicMock, patch
from src.infrastructure.repositories.mongo_agent_config_repository import MongoAgentConfigRepository
from src.domain.entities.agent_config import AgentConfig


class TestMongoAgentConfigRepository:
    """Testes unitários para MongoAgentConfigRepository."""
    
    @patch('src.infrastructure.repositories.mongo_agent_config_repository.MongoClient')
    def test_get_active_agents_returns_list(self, mock_mongo_client):
        """Testa se get_active_agents retorna lista de agentes ativos."""
        # Arrange
        mock_collection = MagicMock()
        mock_collection.find.return_value = [
            {
                "id": "agent1",
                "nome": "Agente 1",
                "factoryIaModel": "ollama",
                "model": "llama3.2:latest",
                "descricao": "Primeiro agente",
                "prompt": "Você é o agente 1",
                "active": True
            },
            {
                "id": "agent2",
                "nome": "Agente 2",
                "factoryIaModel": "ollama",
                "model": "llama3.2:latest",
                "descricao": "Segundo agente",
                "prompt": "Você é o agente 2",
                "active": True
            }
        ]
        
        mock_db = MagicMock()
        mock_db.__getitem__.return_value = mock_collection
        
        mock_client = MagicMock()
        mock_client.__getitem__.return_value = mock_db
        
        mock_mongo_client.return_value = mock_client
        
        repository = MongoAgentConfigRepository()
        
        # Act
        agents = repository.get_active_agents()
        
        # Assert
        assert len(agents) == 2
        assert all(isinstance(agent, AgentConfig) for agent in agents)
        assert agents[0].id == "agent1"
        assert agents[1].id == "agent2"
        mock_collection.find.assert_called_once_with({"active": True})
    
    @patch('src.infrastructure.repositories.mongo_agent_config_repository.MongoClient')
    def test_get_active_agents_returns_empty_list_when_no_agents(self, mock_mongo_client):
        """Testa se get_active_agents retorna lista vazia quando não há agentes."""
        # Arrange
        mock_collection = MagicMock()
        mock_collection.find.return_value = []
        
        mock_db = MagicMock()
        mock_db.__getitem__.return_value = mock_collection
        
        mock_client = MagicMock()
        mock_client.__getitem__.return_value = mock_db
        
        mock_mongo_client.return_value = mock_client
        
        repository = MongoAgentConfigRepository()
        
        # Act
        agents = repository.get_active_agents()
        
        # Assert
        assert len(agents) == 0
        assert isinstance(agents, list)
    
    @patch('src.infrastructure.repositories.mongo_agent_config_repository.MongoClient')
    def test_get_agent_by_id_returns_agent(self, mock_mongo_client):
        """Testa se get_agent_by_id retorna o agente correto."""
        # Arrange
        agent_data = {
            "id": "test-agent",
            "nome": "Agente Teste",
            "factoryIaModel": "ollama",
            "model": "llama3.2:latest",
            "descricao": "Um agente para testes",
            "prompt": "Você é um assistente útil",
            "active": True
        }
        
        mock_collection = MagicMock()
        mock_collection.find_one.return_value = agent_data
        
        mock_db = MagicMock()
        mock_db.__getitem__.return_value = mock_collection
        
        mock_client = MagicMock()
        mock_client.__getitem__.return_value = mock_db
        
        mock_mongo_client.return_value = mock_client
        
        repository = MongoAgentConfigRepository()
        
        # Act
        agent = repository.get_agent_by_id("test-agent")
        
        # Assert
        assert isinstance(agent, AgentConfig)
        assert agent.id == "test-agent"
        assert agent.nome == "Agente Teste"
        mock_collection.find_one.assert_called_once_with({"id": "test-agent"})
    
    @patch('src.infrastructure.repositories.mongo_agent_config_repository.MongoClient')
    def test_get_agent_by_id_raises_error_when_not_found(self, mock_mongo_client):
        """Testa se get_agent_by_id levanta erro quando agente não é encontrado."""
        # Arrange
        mock_collection = MagicMock()
        mock_collection.find_one.return_value = None
        
        mock_db = MagicMock()
        mock_db.__getitem__.return_value = mock_collection
        
        mock_client = MagicMock()
        mock_client.__getitem__.return_value = mock_db
        
        mock_mongo_client.return_value = mock_client
        
        repository = MongoAgentConfigRepository()
        
        # Act & Assert
        with pytest.raises(ValueError, match="Agente com ID inexistente não encontrado"):
            repository.get_agent_by_id("inexistente")
    
    def test_map_to_entity_with_all_fields(self):
        """Testa mapeamento de dados para entidade com todos os campos."""
        # Arrange
        repository = MongoAgentConfigRepository()
        agent_data = {
            "id": "test-agent",
            "nome": "Agente Teste",
            "factoryIaModel": "ollama",
            "model": "llama3.2:latest",
            "descricao": "Um agente para testes",
            "prompt": "Você é um assistente útil",
            "active": False
        }
        
        # Act
        agent = repository._map_to_entity(agent_data)
        
        # Assert
        assert isinstance(agent, AgentConfig)
        assert agent.id == "test-agent"
        assert agent.nome == "Agente Teste"
        assert agent.model == "llama3.2:latest"
        assert agent.descricao == "Um agente para testes"
        assert agent.prompt == "Você é um assistente útil"
        assert agent.active is False
    
    def test_map_to_entity_with_default_active_value(self):
        """Testa mapeamento com valor padrão para active."""
        # Arrange
        repository = MongoAgentConfigRepository()
        agent_data = {
            "id": "test-agent",
            "nome": "Agente Teste",
            "factoryIaModel": "ollama",
            "model": "llama3.2:latest",
            "descricao": "Um agente para testes",
            "prompt": "Você é um assistente útil"
            # Sem campo 'active'
        }
        
        # Act
        agent = repository._map_to_entity(agent_data)
        
        # Assert
        assert agent.active is True  # valor padrão
    
    def test_repository_initialization_with_custom_values(self):
        """Testa inicialização do repositório com valores customizados."""
        # Arrange & Act
        repository = MongoAgentConfigRepository(
            connection_string="mongodb://custom:27017",
            database_name="custom_db",
            collection_name="custom_collection"
        )
        
        # Assert
        assert repository._connection_string == "mongodb://custom:27017"
        assert repository._database_name == "custom_db"
        assert repository._collection_name == "custom_collection"
        assert repository._client is None
        assert repository._db is None
        assert repository._collection is None
