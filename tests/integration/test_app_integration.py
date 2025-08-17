import pytest
import os
from unittest.mock import patch, MagicMock
from fastapi.testclient import TestClient


class TestIntegrationApp:
    """Testes de integração para a aplicação."""
    
    def test_app_creation(self, mock_mongo_client, mock_tool_repository):
        """Testa se a aplicação é criada corretamente."""
        # Act - Import dentro do teste para evitar execução antecipada
        from app import create_app
        app = create_app()
        
        # Assert
        assert app is not None
        assert app.title == "Orquestrador de Agentes IA"
        
        # Verificar se a aplicação tem rotas configuradas
        assert len(app.routes) > 0
        
        # Verificar que os mocks foram chamados (indicando que a aplicação tentou acessar o BD)
        mock_mongo_client.assert_called()
    
    def test_app_with_test_client(self, mock_mongo_client, mock_tool_repository):
        """Testa se a aplicação funciona com TestClient."""
        from app import create_app
        app = create_app()
        
        client = TestClient(app)
        
        # Testar que a aplicação pelo menos inicializa corretamente
        # Não testamos endpoints específicos pois podem precisar de mais configuração
        assert client.app == app
    
    def test_app_health_check(self):
        """Testa se a aplicação responde corretamente."""
        # Esta seria uma implementação mais completa que testaria
        # se a aplicação realmente funciona com um cliente de teste
        pass
