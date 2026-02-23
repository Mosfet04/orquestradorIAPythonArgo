"""Testes unitários para a entidade AgentConfig."""

import pytest
from src.domain.entities.agent_config import AgentConfig


class TestAgentConfig:
    def test_create_with_valid_data(self):
        config = AgentConfig(
            id="test-agent",
            nome="Agente Teste",
            factory_ia_model="ollama",
            model="llama3.2:latest",
            descricao="Um agente para testes",
            prompt="Você é um assistente útil.",
        )
        assert config.id == "test-agent"
        assert config.nome == "Agente Teste"
        assert config.factory_ia_model == "ollama"
        assert config.model == "llama3.2:latest"
        assert config.active is True

    def test_create_with_active_false(self):
        config = AgentConfig(
            id="inactive",
            nome="Inativo",
            factory_ia_model="ollama",
            model="llama3.2:latest",
            descricao="desc",
            prompt="prompt",
            active=False,
        )
        assert config.active is False

    def test_empty_id_raises_error(self):
        with pytest.raises(ValueError, match="ID do agente não pode estar vazio"):
            AgentConfig(
                id="", nome="A", factory_ia_model="ollama",
                model="m", descricao="d", prompt="p",
            )

    def test_empty_nome_raises_error(self):
        with pytest.raises(ValueError, match="Nome do agente não pode estar vazio"):
            AgentConfig(
                id="x", nome="", factory_ia_model="ollama",
                model="m", descricao="d", prompt="p",
            )

    def test_empty_model_raises_error(self):
        with pytest.raises(ValueError, match="Modelo do agente não pode estar vazio"):
            AgentConfig(
                id="x", nome="A", factory_ia_model="ollama",
                model="", descricao="d", prompt="p",
            )

    def test_empty_factory_model_raises_error(self):
        with pytest.raises(ValueError, match="Factory do modelo do agente não pode estar vazio"):
            AgentConfig(
                id="x", nome="A", factory_ia_model="",
                model="m", descricao="d", prompt="p",
            )
