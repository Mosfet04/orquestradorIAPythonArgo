import pytest
from src.domain.entities.agent_config import AgentConfig


class TestAgentConfig:
    """Testes unitários para a entidade AgentConfig."""
    
    def test_create_agent_config_with_valid_data(self):
        """Testa criação de AgentConfig com dados válidos."""
        # Arrange & Act
        config = AgentConfig(
            id="test-agent",
            nome="Agente Teste",
            factoryIaModel="ollama",
            model="llama3.2:latest",
            descricao="Um agente para testes",
            prompt="Você é um assistente útil."
        )
        
        # Assert
        assert config.id == "test-agent"
        assert config.nome == "Agente Teste"
        assert config.model == "llama3.2:latest"
        assert config.descricao == "Um agente para testes"
        assert config.prompt == "Você é um assistente útil."
        assert config.active is True  # default value
    
    def test_create_agent_config_with_active_false(self):
        """Testa criação de AgentConfig com active=False."""
        # Arrange & Act
        config = AgentConfig(
            id="inactive-agent",
            nome="Agente Inativo",
            factoryIaModel="ollama",
            model="llama3.2:latest",
            descricao="Um agente inativo",
            prompt="Prompt do agente inativo",
            active=False
        )
        
        # Assert
        assert config.active is False
    
    def test_agent_config_with_empty_id_raises_error(self):
        """Testa se ID vazio levanta ValueError."""
        # Arrange & Act & Assert
        with pytest.raises(ValueError, match="ID do agente não pode estar vazio"):
            AgentConfig(
                id="",
                nome="Agente Teste",
                factoryIaModel="ollama",
                model="llama3.2:latest",
                descricao="Descrição",
                prompt="Prompt"
            )
    
    def test_agent_config_with_empty_nome_raises_error(self):
        """Testa se nome vazio levanta ValueError."""
        # Arrange & Act & Assert
        with pytest.raises(ValueError, match="Nome do agente não pode estar vazio"):
            AgentConfig(
                id="test-agent",
                nome="",
                factoryIaModel="ollama",
                model="llama3.2:latest",
                descricao="Descrição",
                prompt="Prompt"
            )
    
    def test_agent_config_with_empty_model_raises_error(self):
        """Testa se model vazio levanta ValueError."""
        # Arrange & Act & Assert
        with pytest.raises(ValueError, match="Modelo do agente não pode estar vazio"):
            AgentConfig(
                id="test-agent",
                nome="Agente Teste",
                factoryIaModel="ollama",
                model="",
                descricao="Descrição",
                prompt="Prompt"
            )
    def test_agent_config_with_empty_factory_model_raises_error(self):
        """Testa se factoryIaModel vazio levanta ValueError."""
        # Arrange & Act & Assert
        with pytest.raises(ValueError, match="Factory do modelo do agente não pode estar vazio"):
            AgentConfig(
                id="test-agent",
                nome="Agente Teste",
                factoryIaModel="",
                model="llama3.2:latest",
                descricao="Descrição",
                prompt="Prompt"
            )
