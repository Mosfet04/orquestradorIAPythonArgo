import pytest
from src.application.services.agent_factory_service import AgentFactoryService
from src.domain.entities.agent_config import AgentConfig


class TestAgentFactoryService:
    """Testes unitários para o AgentFactoryService."""
    
    def test_create_agent_with_valid_ollama_config(self):
        """Testa a criação de agente com configuração Ollama válida."""
        # Arrange
        config = AgentConfig(
            id="test-agent",
            nome="Agente Teste",
            factoryIaModel="ollama",
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
        assert agent.description == config.descricao
    
    def test_create_agent_with_invalid_factory_model_raises_error(self):
        """Testa se factory model inválido levanta erro."""
        # Arrange
        config = AgentConfig(
            id="test-agent",
            nome="Agente Teste",
            factoryIaModel="invalid_model",
            model="llama3.2:latest",
            descricao="Um agente para testes",
            prompt="Você é um assistente útil."
        )
        
        service = AgentFactoryService()
        
        # Act & Assert
        with pytest.raises(ValueError, match="Configuração de modelo inválida"):
            service.create_agent(config)
    
    def test_create_agent_with_empty_model_id_raises_error(self):
        """Testa se model ID vazio levanta erro na criação do AgentConfig."""
        # Act & Assert - O erro deve ser lançado na criação do AgentConfig
        with pytest.raises(ValueError, match="Modelo do agente não pode estar vazio"):
            AgentConfig(
                id="test-agent",
                nome="Agente Teste",
                factoryIaModel="ollama",
                model="",
                descricao="Um agente para testes",
                prompt="Você é um assistente útil."
            )
