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


@pytest.mark.parametrize("field", ["id", "nome", "model", "factory_ia_model"])
@pytest.mark.parametrize("value", [7, ["x"], {"$gt": ""}, True])
def test_campo_de_texto_com_outro_tipo_e_recusado_sem_ecoar_o_valor(field, value):
    """Documento do Mongo sem schema (F1-10): tipo errado quebrava o AgentOS na montagem."""
    kwargs = {"id": "a", "nome": "A", "factory_ia_model": "ollama", "model": "m", "descricao": "d", "prompt": "p"}
    with pytest.raises(ValueError, match="deve ser texto") as raised:
        AgentConfig(**{**kwargs, field: value})
    assert str(value) not in str(raised.value)


@pytest.mark.parametrize("value", [7, ["x"], {"pt": "x"}, True])
def test_descricao_com_outro_tipo_e_recusada(value):
    """``descricao`` vai para o ``AgentResponse.description: str`` do AgentOS: tipo errado dava 500
    em ``GET /agents`` e ``/config`` para todos os agentes (F1-10)."""
    kwargs = {"id": "a", "nome": "A", "factory_ia_model": "ollama", "model": "m", "prompt": "p"}
    with pytest.raises(ValueError, match="Descrição do agente deve ser texto"):
        AgentConfig(**kwargs, descricao=value)


@pytest.mark.parametrize("value", [None, "", "texto"])
def test_descricao_opcional_aceita_none_e_texto(value):
    kwargs = {"id": "a", "nome": "A", "factory_ia_model": "ollama", "model": "m", "prompt": "p"}
    assert AgentConfig(**kwargs, descricao=value).descricao == value
