"""Testes para a entidade TeamConfig."""

from __future__ import annotations

import pytest

from src.domain.entities.team_config import TeamConfig
from src.domain.entities.validation import InvalidField


def _valid_kwargs(**overrides):
    """Retorna kwargs válidos para TeamConfig, com overrides opcionais."""
    base = {
        "id": "team-1",
        "nome": "Team de Suporte",
        "factory_ia_model": "ollama",
        "model": "llama3.2:latest",
        "member_ids": ["agent-a", "agent-b"],
    }
    base.update(overrides)
    return base


class TestTeamConfigCreation:
    def test_create_valid_team_config(self):
        tc = TeamConfig(**_valid_kwargs())
        assert tc.id == "team-1"
        assert tc.nome == "Team de Suporte"
        assert tc.mode == "route"
        assert tc.user_memory_active is True
        assert tc.summary_active is False
        assert tc.active is True
        assert tc.descricao is None
        assert tc.prompt is None

    def test_all_modes_accepted(self):
        for mode in ("route", "coordinate", "broadcast", "tasks"):
            tc = TeamConfig(**_valid_kwargs(mode=mode))
            assert tc.mode == mode

    def test_custom_defaults_overridden(self):
        tc = TeamConfig(
            **_valid_kwargs(
                user_memory_active=False,
                summary_active=True,
                active=False,
                descricao="Desc",
                prompt="Prompt",
            )
        )
        assert tc.user_memory_active is False
        assert tc.summary_active is True
        assert tc.active is False
        assert tc.descricao == "Desc"
        assert tc.prompt == "Prompt"


class TestTeamConfigValidation:
    def test_empty_id_raises(self):
        with pytest.raises(ValueError, match="ID do team"):
            TeamConfig(**_valid_kwargs(id=""))

    def test_empty_nome_raises(self):
        with pytest.raises(ValueError, match="Nome do team"):
            TeamConfig(**_valid_kwargs(nome=""))

    def test_empty_model_raises(self):
        with pytest.raises(ValueError, match="Modelo do team"):
            TeamConfig(**_valid_kwargs(model=""))

    def test_empty_factory_raises(self):
        with pytest.raises(ValueError, match="Factory do modelo"):
            TeamConfig(**_valid_kwargs(factory_ia_model=""))

    def test_empty_member_ids_raises(self):
        with pytest.raises(ValueError, match="pelo menos um membro"):
            TeamConfig(**_valid_kwargs(member_ids=[]))

    def test_invalid_mode_raises(self):
        with pytest.raises(ValueError, match="Modo inválido"):
            TeamConfig(**_valid_kwargs(mode="invalid"))


@pytest.mark.parametrize(
    ("overrides", "field"),
    [
        ({"mode": "valor-recebido-x"}, "mode"),
        ({"member_ids": []}, "member_ids"),
        ({"nome": ["valor-recebido-x"]}, "nome"),
        ({"api_key_ref": "valor-recebido-x"}, "api_key_ref"),
        ({"base_url": "ftp://valor-recebido-x"}, "base_url"),
        ({"model_params": {"t": ["valor-recebido-x"]}}, "model_params"),
    ],
)
def test_recusa_diz_o_campo_do_documento_sem_ecoar_o_valor(overrides, field):
    """FA-02: a API responde 422 com o campo; a mensagem (que vai a log) não cita o valor."""
    with pytest.raises(InvalidField) as raised:
        TeamConfig(**_valid_kwargs(**overrides))
    assert raised.value.field == field
    assert "valor-recebido-x" not in str(raised.value)


@pytest.mark.parametrize("field", ["id", "nome", "model", "factory_ia_model"])
@pytest.mark.parametrize("value", [7, ["x"], {"$gt": ""}, True])
def test_campo_de_texto_com_outro_tipo_e_recusado_sem_ecoar_o_valor(field, value):
    """Documento do Mongo sem schema (F1-10): tipo errado quebrava o AgentOS na montagem."""
    with pytest.raises(ValueError, match="deve ser texto") as raised:
        TeamConfig(**_valid_kwargs(**{field: value}))
    assert str(value) not in str(raised.value)


@pytest.mark.parametrize("value", [7, ["x"], {"pt": "x"}, True])
def test_descricao_com_outro_tipo_e_recusada(value):
    """``descricao`` vai para o ``TeamResponse.description`` do AgentOS (F1-10)."""
    with pytest.raises(ValueError, match="Descrição do team deve ser texto"):
        TeamConfig(**_valid_kwargs(descricao=value))


@pytest.mark.parametrize("value", [None, "", "texto"])
def test_descricao_opcional_aceita_none_e_texto(value):
    assert TeamConfig(**_valid_kwargs(descricao=value)).descricao == value


# FA-07: o que a montagem fixava no ``agno.Team`` vira campo opcional, com o default de antes.
_TEAM_FLAGS = ("markdown", "add_history_to_context")


def test_campos_de_execucao_ausentes_tem_o_default_do_comportamento_anterior():
    config = TeamConfig(**_valid_kwargs())

    assert (config.markdown, config.add_history_to_context) == (True, True)


@pytest.mark.parametrize("field", _TEAM_FLAGS)
@pytest.mark.parametrize("value", [1, 0, "false", "valor-recebido-x", None])
def test_flag_de_execucao_so_aceita_bool(field, value):
    with pytest.raises(InvalidField) as raised:
        TeamConfig(**_valid_kwargs(**{field: value}))
    assert raised.value.field == field
    assert "valor-recebido-x" not in str(raised.value)


@pytest.mark.parametrize("field", _TEAM_FLAGS)
def test_flag_de_execucao_aceita_os_dois_valores(field):
    assert [getattr(TeamConfig(**_valid_kwargs(**{field: v})), field) for v in (True, False)] == [True, False]
