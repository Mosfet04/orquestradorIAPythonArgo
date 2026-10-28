"""FA-03: montador do schema de ``/admin/schema`` a partir do código em vigor.

Providers vêm do ``ProviderRegistry`` (built-ins e plugins, pelo mesmo filtro do ``supported``); modos de
team, métodos HTTP, tipos de parâmetro, ``search_strategy`` e formatos de ``api_key_ref`` vêm do domínio;
o composition root diz se o store é gravável.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.application.use_cases.config_schema import ConfigSchema, ProviderOption
from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.model_config import parse_secret_ref
from src.domain.entities.team_config import TeamConfig
from src.domain.entities.validation import InvalidField
from src.infrastructure.dependency_injection import build_config_schema
from src.infrastructure.providers import ClassSpec, ProviderRegistry, ProviderSpec
from src.infrastructure.providers.builtins import BUILTIN_PROVIDERS


def _schema(registry: ProviderRegistry | None = None, *, writable: bool = True) -> dict[str, Any]:
    schema = build_config_schema(registry or ProviderRegistry(BUILTIN_PROVIDERS), writable=writable)
    return json.loads(json.dumps(schema.document()))


def _by_name(options: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {option["name"]: option for option in options}


def test_documento_lista_as_opcoes_de_provider_recebidas_por_tipo() -> None:
    schema = ConfigSchema(
        providers={"chat": (ProviderOption(name="x", model_params=("a", "b"), requires_base_url=True),)},
        config_store_writable=False,
    )

    assert schema.document()["providers"] == {
        "chat": [{"name": "x", "model_params": ["a", "b"], "requires_base_url": True}]
    }


def test_providers_por_tipo_sao_os_ids_e_aliases_do_registry() -> None:
    registry = ProviderRegistry(BUILTIN_PROVIDERS)

    providers = _schema(registry)["providers"]

    assert [option["name"] for option in providers["chat"]] == registry.supported("chat")
    assert [option["name"] for option in providers["embedder"]] == registry.supported("embedder")
    assert {"anthropic", "groq"}.isdisjoint(option["name"] for option in providers["embedder"])


def test_model_params_e_base_url_obrigatoria_vem_da_spec_do_provider() -> None:
    providers = _schema()["providers"]
    chat, embedder = _by_name(providers["chat"]), _by_name(providers["embedder"])

    assert chat["anthropic"] == {
        "name": "anthropic",
        "model_params": ["max_tokens", "temperature", "timeout", "top_k", "top_p"],
        "requires_base_url": False,
    }
    assert chat["google"] == {**chat["gemini"], "name": "google"}
    assert chat["openai_compatible"]["requires_base_url"] is True
    assert embedder["openai_compatible"]["requires_base_url"] is True
    assert embedder["ollama"]["model_params"] == ["dimensions", "timeout"]
    assert "num_ctx" in chat["ollama"]["model_params"]


def test_provider_registrado_depois_aparece_no_schema_sem_mudar_o_montador() -> None:
    registry = ProviderRegistry(BUILTIN_PROVIDERS)
    registry.register(
        ProviderSpec(
            id="acme",
            sdk_package="acme-sdk",
            aliases=("acme-ai",),
            chat=ClassSpec(class_path="acme.Chat", params={"temperature": "temperature"}),
            requires_base_url=True,
        )
    )

    providers = _schema(registry)["providers"]

    chat = _by_name(providers["chat"])
    assert chat["acme"] == {"name": "acme", "model_params": ["temperature"], "requires_base_url": True}
    assert chat["acme-ai"] == {**chat["acme"], "name": "acme-ai"}
    assert "acme" not in _by_name(providers["embedder"])


def test_enumeracoes_sao_as_das_entidades() -> None:
    schema = _schema()

    assert schema["team"]["mode"] == ["route", "coordinate", "broadcast", "tasks"]
    assert schema["tool"] == {
        "http_method": ["GET", "POST", "PUT", "DELETE", "PATCH"],
        "parameter_type": ["string", "integer", "float", "boolean", "object", "array"],
    }
    assert schema["rag_config"] == {"search_strategy": ["semantic", "hierarchical"]}


def test_cada_modo_de_team_do_schema_e_aceito_pela_entidade_e_outro_nao() -> None:
    def team(mode: str) -> TeamConfig:
        return TeamConfig(id="t", nome="T", factory_ia_model="ollama", model="m", member_ids=["a"], mode=mode)

    assert [team(mode).mode for mode in _schema()["team"]["mode"]] == ["route", "coordinate", "broadcast", "tasks"]
    with pytest.raises(InvalidField) as caught:
        team("sequential")
    assert caught.value.field == "mode"


def test_formatos_de_referencia_de_segredo_preenchidos_sao_aceitos_pelo_dominio() -> None:
    formats = _schema()["api_key_ref"]["formats"]

    assert formats == ["env:<PROVEDOR>_API_KEY", "file:<caminho em SECRETS_DIR>"]
    filled = [
        ref.replace("<PROVEDOR>", "OPENAI").replace("<caminho em SECRETS_DIR>", "/run/secrets/openai")
        for ref in formats
    ]
    assert [parse_secret_ref(ref) for ref in filled] == [("env", "OPENAI_API_KEY"), ("file", "/run/secrets/openai")]


@pytest.mark.parametrize("writable", [True, False])
def test_config_store_gravavel_ou_so_leitura(writable: bool) -> None:
    assert _schema(writable=writable)["config_store"] == {"writable": writable}


def test_campos_de_execucao_com_tipo_default_e_faixa_da_entidade() -> None:
    """FA-07: o que a montagem fixava; o default é o da entidade (o comportamento de antes)."""
    schema = _schema()

    assert schema["agent"] == {
        "markdown": {"type": "boolean", "default": True},
        "add_history_to_context": {"type": "boolean", "default": True},
        "add_datetime_to_context": {"type": "boolean", "default": True},
        "num_history_runs": {"type": "integer", "default": 5, "minimum": 1, "maximum": 50},
        "reasoning": {"type": "boolean", "default": False},
    }
    assert {key: value for key, value in schema["team"].items() if key != "mode"} == {
        "markdown": {"type": "boolean", "default": True},
        "add_history_to_context": {"type": "boolean", "default": True},
    }


def test_faixa_de_num_history_runs_do_schema_e_a_que_a_entidade_aceita() -> None:
    entry = _schema()["agent"]["num_history_runs"]

    def agent(runs: object) -> AgentConfig:
        return AgentConfig(
            id="a", nome="A", factory_ia_model="ollama", model="m", descricao=None, prompt="p",
            num_history_runs=runs,  # type: ignore[arg-type]
        )

    assert [agent(runs).num_history_runs for runs in (entry["minimum"], entry["maximum"])] == [1, 50]
    for runs in (entry["minimum"] - 1, entry["maximum"] + 1):
        with pytest.raises(InvalidField) as caught:
            agent(runs)
        assert caught.value.field == "num_history_runs"
