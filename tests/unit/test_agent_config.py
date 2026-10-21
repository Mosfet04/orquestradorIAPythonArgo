"""Testes unitários para a entidade AgentConfig."""

import pytest

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.validation import InvalidField


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


# FA-07: o que a montagem fixava no ``agno.Agent`` vira campo opcional, com o default de antes.
_AGENT_KWARGS = {"id": "a", "nome": "A", "factory_ia_model": "ollama", "model": "m", "descricao": "d", "prompt": "p"}
_AGENT_FLAGS = ("markdown", "add_history_to_context", "add_datetime_to_context", "reasoning")


def test_campos_de_execucao_ausentes_tem_o_default_do_comportamento_anterior():
    config = AgentConfig(**_AGENT_KWARGS)

    assert (
        config.markdown,
        config.add_history_to_context,
        config.add_datetime_to_context,
        config.num_history_runs,
        config.reasoning,
    ) == (True, True, True, 5, False)


@pytest.mark.parametrize("field", _AGENT_FLAGS)
@pytest.mark.parametrize("value", [1, 0, "true", "valor-recebido-x", None, [True]])
def test_flag_de_execucao_so_aceita_bool(field, value):
    """``"false"`` é verdadeiro em Python: coagir ligaria o recurso que o documento quis desligar."""
    with pytest.raises(InvalidField) as raised:
        AgentConfig(**_AGENT_KWARGS, **{field: value})
    assert raised.value.field == field
    assert "valor-recebido-x" not in str(raised.value)


@pytest.mark.parametrize("field", _AGENT_FLAGS)
def test_flag_de_execucao_aceita_os_dois_valores(field):
    assert [getattr(AgentConfig(**_AGENT_KWARGS, **{field: v}), field) for v in (True, False)] == [True, False]


@pytest.mark.parametrize("value", [1, 50])
def test_num_history_runs_aceita_a_faixa(value):
    assert AgentConfig(**_AGENT_KWARGS, num_history_runs=value).num_history_runs == value


@pytest.mark.parametrize("value", [0, -1, 51, True, False, "5", 5.0, None, "valor-recebido-x"])
def test_num_history_runs_fora_da_faixa_ou_nao_inteiro_e_recusado(value):
    """0 no agno é o histórico inteiro (``runs[-0:]``): recusado; quem não quer histórico desliga a flag."""
    with pytest.raises(InvalidField) as raised:
        AgentConfig(**_AGENT_KWARGS, num_history_runs=value)
    assert raised.value.field == "num_history_runs"
    assert "valor-recebido-x" not in str(raised.value)
