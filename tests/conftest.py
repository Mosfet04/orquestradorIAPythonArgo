"""
Configurações compartilhadas para todos os testes.
"""

import pytest
import os
from unittest.mock import patch, MagicMock


@pytest.fixture(scope="session", autouse=True)
def setup_test_environment():
    """Configura o ambiente de teste."""
    # Configurar variáveis de ambiente para testes
    test_env = {
        'MONGO_CONNECTION_STRING': os.getenv('MONGO_CONNECTION_STRING'),
        'MONGO_DATABASE_NAME': os.getenv('MONGO_DATABASE_NAME'),
        'APP_TITLE': 'Orquestrador de Agentes IA',
        'LOG_LEVEL': 'ERROR'  # Reduzir logs durante testes
    }
    
    with patch.dict(os.environ, test_env):
        yield


@pytest.fixture
def mock_mongo_client():
    """Mock para cliente MongoDB."""
    with patch('src.infrastructure.repositories.mongo_agent_config_repository.MongoClient') as mock_client:
        # Configurar mock
        mock_collection = MagicMock()
        mock_db = MagicMock()
        mock_client_instance = MagicMock()
        
        mock_client.return_value = mock_client_instance
        mock_client_instance.__getitem__.return_value = mock_db
        mock_db.__getitem__.return_value = mock_collection
        
        # Configurar dados de teste padrão
        mock_collection.find.return_value = [
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
        
        mock_collection.find_one.return_value = {
            "id": "test-agent-1",
            "nome": "Agente Teste 1",
            "model": "llama3.2:latest",
            "descricao": "Primeiro agente de teste",
            "prompt": "Você é um assistente útil.",
            "active": True
        }
        
        yield mock_client


@pytest.fixture
def mock_tool_repository():
    """Mock para repositório de ferramentas."""
    with patch('src.infrastructure.repositories.mongo_tool_repository.MongoClient') as mock_client:
        # Configurar mock similar ao acima
        mock_collection = MagicMock()
        mock_db = MagicMock()
        mock_client_instance = MagicMock()
        
        mock_client.return_value = mock_client_instance
        mock_client_instance.__getitem__.return_value = mock_db
        mock_db.__getitem__.return_value = mock_collection
        
        # Dados de teste para ferramentas
        mock_collection.find.return_value = []
        
        yield mock_client
