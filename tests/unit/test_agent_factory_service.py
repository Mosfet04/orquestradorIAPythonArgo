import pytest
from unittest.mock import Mock
from src.application.services.agent_factory_service import AgentFactoryService
from src.domain.entities.agent_config import AgentConfig


class TestAgentFactoryService:
    """Testes unitários para o AgentFactoryService."""
    
    def test_create_agent_with_valid_config(self):
        """Testa a criação de agente com configuração válida."""
        # Arrange
        config = AgentConfig(
            id="test-agent",
            nome="Agente Teste",
            model="llama3.2:latest",
            descricao="Um agente para testes",
            prompt="Você é um assistente útil."
        )
        
        service = AgentFactoryService()
        
        # Act
        agent = service.create_agent(config)
        
        # Assert
        assert agent is not None
        assert agent.name == config.nome
        assert agent.agent_id == config.id
        assert agent.description == config.descricao
