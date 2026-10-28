"""QA do FA-03: o schema anunciado bate com o que o registry e as entidades aceitam de fato.

Cada ``model_param`` anunciado passa pela allowlist do registry e um não anunciado é recusado; ``requires_base_url``
é o que ``create_model``/``create_embedder`` cobram; aliases espelham o provider; o documento é novo a cada chamada.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from src.domain.entities.model_config import ModelConfig
from src.domain.ports.model_factory_port import InvalidModelConfigError
from src.infrastructure.dependency_injection import build_config_schema
from src.infrastructure.providers import DestinationPolicy, ProviderRegistry
from src.infrastructure.providers.builtins import BUILTIN_PROVIDERS
from src.infrastructure.providers.registry import Kind

KINDS: tuple[Kind, ...] = ("chat", "embedder")
NOT_ALLOWED = "chave não permitida"


@pytest.fixture(autouse=True)
def _clean_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Nenhuma chave nem variável de operador real do shell entra nos ``create_*`` destes testes."""
    names = {spec.api_key_env for spec in BUILTIN_PROVIDERS if spec.api_key_env}
    for spec in BUILTIN_PROVIDERS:
        for class_spec in (spec.chat, spec.embedder):
            names.update(item.env for item in (class_spec.operator_env if class_spec else ()))
    for name in names:
        monkeypatch.delenv(name, raising=False)


def _registry() -> ProviderRegistry:
    # Loopback liberado e resolvedor falso: a base_url passa pela guarda sem rede.
    return ProviderRegistry(
        BUILTIN_PROVIDERS, policy=DestinationPolicy(allow_loopback=True, resolver=lambda host: ["127.0.0.1"])
    )


def _create(
    registry: ProviderRegistry, kind: Kind, provider: str, params: dict[str, Any], base_url: str | None
) -> None:
    config = ModelConfig(provider=provider, model_id="m", params=params, base_url=base_url)
    (registry.create_model if kind == "chat" else registry.create_embedder)(config)


def _cases() -> list[tuple[Kind, str]]:
    registry = ProviderRegistry(BUILTIN_PROVIDERS)
    return [(kind, name) for kind in KINDS for name in registry.supported(kind)]


@pytest.mark.parametrize(("kind", "name"), _cases())
def test_model_params_anunciados_passam_pela_allowlist_e_um_nao_anunciado_e_recusado(kind: Kind, name: str) -> None:
    registry = _registry()
    option = next(o for o in build_config_schema(registry, writable=True).providers[kind] if o.name == name)
    base_url = "http://127.0.0.1:9" if option.requires_base_url else None

    for param in option.model_params:
        try:
            _create(registry, kind, name, {param: 1}, base_url)
        except InvalidModelConfigError as exc:
            # Pode falhar adiante (SDK ausente, chave); nunca pela allowlist de params.
            assert NOT_ALLOWED not in str(exc), f"{name}/{kind}: {param} anunciado e recusado"
    with pytest.raises(InvalidModelConfigError, match=NOT_ALLOWED):
        _create(registry, kind, name, {"param_que_nao_existe": 1}, base_url)


@pytest.mark.parametrize(("kind", "name"), _cases())
def test_requires_base_url_e_o_que_o_registry_cobra_sem_url_do_operador(kind: Kind, name: str) -> None:
    registry = _registry()
    option = next(o for o in build_config_schema(registry, writable=True).providers[kind] if o.name == name)

    try:
        _create(registry, kind, name, {}, None)
    except InvalidModelConfigError as exc:
        demanded = "exige base_url" in str(exc)
    else:
        demanded = False

    assert demanded is option.requires_base_url


def test_cada_alias_espelha_o_provider_de_origem_nos_dois_tipos() -> None:
    providers = build_config_schema(ProviderRegistry(BUILTIN_PROVIDERS), writable=True).providers
    checked = 0
    for spec in BUILTIN_PROVIDERS:
        for kind in KINDS:
            by_name = {o.name: o for o in providers[kind]}
            if spec.id not in by_name:
                assert all(alias not in by_name for alias in spec.aliases)
                continue
            for alias in spec.aliases:
                checked += 1
                assert by_name[alias].model_params == by_name[spec.id].model_params
                assert by_name[alias].requires_base_url == by_name[spec.id].requires_base_url
    assert checked > 0


def test_cada_nome_anunciado_e_um_provider_do_registry_e_nomes_sao_unicos_e_ordenados() -> None:
    registry = ProviderRegistry(BUILTIN_PROVIDERS)
    providers = build_config_schema(registry, writable=True).providers

    for kind in KINDS:
        names = [o.name for o in providers[kind]]
        assert names == sorted(set(names))
        assert all(name in registry for name in names)
        assert all(list(o.model_params) == sorted(o.model_params) for o in providers[kind])


def test_documento_e_novo_a_cada_chamada_e_mutar_o_retorno_nao_altera_o_schema() -> None:
    schema = build_config_schema(ProviderRegistry(BUILTIN_PROVIDERS), writable=True)
    first = schema.document()
    snapshot = json.dumps(first, sort_keys=True)

    first["team"]["mode"].append("hack")  # type: ignore[index, attr-defined]
    first["providers"]["chat"].clear()  # type: ignore[index, attr-defined]
    first["config_store"]["writable"] = False  # type: ignore[index]

    assert json.dumps(schema.document(), sort_keys=True) == snapshot


def test_documento_nao_expoe_detalhe_interno_da_spec() -> None:
    text = json.dumps(build_config_schema(ProviderRegistry(BUILTIN_PROVIDERS), writable=True).document())

    for spec in BUILTIN_PROVIDERS:
        for chunk in (spec.api_key_env, *spec.default_hosts):  # sdk_package pode coincidir com o nome do provider
            assert not chunk or chunk not in text, chunk
        for class_spec in (spec.chat, spec.embedder):
            assert class_spec is None or class_spec.class_path not in text
    assert "class_path" not in text and "api_key_env" not in text
