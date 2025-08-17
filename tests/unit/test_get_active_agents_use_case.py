import pytest
from unittest.mock import Mock, MagicMock
from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase
from src.domain.entities.agent_config import AgentConfig


class TestGetActiveAgentsUseCase:
    """Testes unitários para o GetActiveAgentsUseCase."""
    
    def test_execute_returns_active_agents(self):
        """Testa se o caso de uso retorna agentes ativos."""
        # Arrange
        mock_repository = Mock()
        mock_service = Mock()
        
        config1 = AgentConfig(
            id="agent1",
            nome="Agente 1",
            factoryIaModel="ollama",
            model="llama3.2:latest",
            descricao="Primeiro agente",
            prompt="Você é o agente 1"
        )
        
        config2 = AgentConfig(
            id="agent2",
            nome="Agente 2",
            factoryIaModel="ollama",
            model="llama3.2:latest",
            descricao="Segundo agente",
            prompt="Você é o agente 2"
        )
        
        mock_repository.get_active_agents.return_value = [config1, config2]
        mock_agent1 = Mock()
        mock_agent2 = Mock()
        mock_service.create_agent.side_effect = [mock_agent1, mock_agent2]

        use_case = GetActiveAgentsUseCase(mock_service, mock_repository)  # Corrigindo ordem dos parâmetros        # Act
        result = use_case.execute()
        
        # Assert
        assert len(result) == 2
        assert result[0] == mock_agent1
        assert result[1] == mock_agent2
        mock_repository.get_active_agents.assert_called_once()
        assert mock_service.create_agent.call_count == 2
