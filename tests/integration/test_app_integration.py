import pytest
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient
from app import create_app
from src.domain.entities.agent_config import AgentConfig


class TestIntegrationApp:
    """Testes de integração para a aplicação."""
    
    @patch('src.infrastructure.repositories.mongo_agent_config_repository.MongoClient')
    def test_app_creation(self, mock_mongo_client):
        """Testa se a aplicação é criada corretamente."""
        # Arrange
        mock_collection = MagicMock()
        
        # Criar dados de teste para agentes ativos
        agent_data = [
            {
                "id": "test-agent-1",
                "nome": "Agente Teste 1",
                "model": "llama3.2:latest",
                "descricao": "Primeiro agente de teste",
                "prompt": "Você é um assistente útil.",
                "active": True
            },
            {
                "id": "test-agent-2", 
                "nome": "Agente Teste 2",
                "model": "llama3.2:latest",
                "descricao": "Segundo agente de teste",
                "prompt": "Você é um especialista em testes.",
                "active": True
            }
        ]
        
        mock_collection.find.return_value = agent_data
        
        mock_db = MagicMock()
        mock_db.__getitem__.return_value = mock_collection
        
        mock_client = MagicMock()
        mock_client.__getitem__.return_value = mock_db
        
        mock_mongo_client.return_value = mock_client
        
        # Act
        app = create_app()
        
        # Assert
        assert app is not None
        assert app.title == "Orquestrador agno"
        
        # Verificar se as rotas foram montadas
        routes = [route.path for route in app.routes]
        assert "/playground" in routes
        assert "/api" in routes
    
    def test_app_health_check(self):
        """Testa se a aplicação responde corretamente."""
        # Esta seria uma implementação mais completa que testaria
        # se a aplicação realmente funciona com um cliente de teste
        pass
