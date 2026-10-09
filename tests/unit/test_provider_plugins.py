"""F2-03: loader de plugins de provider (``orquestrador.providers``).

Critérios: duplicata (plugin x plugin, plugin x built-in, por id ou alias) recusa com mensagem
clara; plugin permitido registra no mesmo registry dos built-ins e é usável, passando pela
mesma guarda de destino e de chave do F2-02 (sem atalho); log de auditoria por plugin.
Distribuições falsas em ``tests/fakes/plugins.py``, descobertas pelo ``importlib.metadata`` real.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.domain.entities.model_config import ModelConfig
from src.domain.ports.model_factory_port import InvalidModelConfigError
from src.infrastructure.providers import DestinationPolicy, ProviderRegistry
from src.infrastructure.providers.builtins import BUILTIN_PROVIDERS
from src.infrastructure.providers.plugins import (
    ENTRY_POINT_GROUP,
    PluginLoadError,
    load_provider_plugins,
    normalize_distribution_name,
)
from tests.fakes import RecordingLogger
from tests.fakes.plugins import FakeSite, spec_module
from tests.fakes.providers import RecordingChatModel, RecordingEmbedder

ACME_KEY = "chave-acme-de-teste"


def _registry(**kwargs: object) -> ProviderRegistry:
    return ProviderRegistry(BUILTIN_PROVIDERS, **kwargs)  # type: ignore[arg-type]


def test_grupo_de_entry_point_e_o_nome_publico_documentado() -> None:
    assert ENTRY_POINT_GROUP == "orquestrador.providers"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [("Acme_Plugin", "acme-plugin"), ("acme.plugin", "acme-plugin"), ("ACME--__..plugin", "acme-plugin"), ("x", "x")],
)
def test_nome_de_distribuicao_normalizado_pela_pep_503(raw: str, expected: str) -> None:
    assert normalize_distribution_name(raw) == expected


# ── plugin permitido: registra e é usável, sem atalho ─────────────────


@pytest.fixture
def acme(plugin_site: FakeSite) -> FakeSite:
    module = spec_module("acme", "acme-ai")
    plugin_site.install("Acme.Plugin", {"acme": "acme_spec_mod:SPEC"}, {"acme_spec_mod": module}, version="1.2.3")
    return plugin_site


def _resolver(addresses: dict[str, str]):
    def resolve(host: str) -> list[str]:
        return [addresses[host]]

    return resolve


@pytest.mark.usefixtures("acme")
def test_plugin_permitido_registra_e_cria_modelo_e_embedder(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACME_API_KEY", ACME_KEY)
    registry = _registry()
    logger = RecordingLogger()

    load_provider_plugins(registry, allowlist=("acme-plugin:acme",), logger=logger)

    model = registry.create_model(ModelConfig("ACME-AI", "acme-1", params={"temperature": 0.3}))
    embedder = registry.create_embedder(ModelConfig("acme", "acme-embed"))
    assert isinstance(model, RecordingChatModel)
    assert model.kwargs == {"id": "acme-1", "temperature": 0.3, "api_key": ACME_KEY}
    assert isinstance(embedder, RecordingEmbedder)
    assert embedder.kwargs == {"id": "acme-embed", "api_key": ACME_KEY}
    assert {"acme", "acme-ai"} <= set(registry.supported("chat"))
    assert [(r.level, r.message, r.context) for r in logger.records] == [
        (
            "info",
            "Plugin de provider carregado",
            {"distribution": "Acme.Plugin", "version": "1.2.3", "entry_point": "acme", "provider_id": "acme"},
        )
    ]


@pytest.mark.usefixtures("acme")
@pytest.mark.parametrize("kind", ["chat", "embedder"])
def test_plugin_passa_pela_guarda_de_destino_do_registry(monkeypatch: pytest.MonkeyPatch, kind: str) -> None:
    monkeypatch.setenv("ACME_API_KEY", ACME_KEY)
    policy = DestinationPolicy(
        allowlist=("gw.permitido.example",),
        resolver=_resolver(
            {"api.acme.example": "203.0.113.10", "gw.permitido.example": "10.1.2.3", "fora.example": "10.9.9.9"}
        ),
    )
    registry = _registry(policy=policy)
    load_provider_plugins(registry, allowlist=("acme-plugin:acme",), logger=RecordingLogger())
    create = registry.create_model if kind == "chat" else registry.create_embedder

    assert create(ModelConfig("acme", "m", base_url="https://api.acme.example/v1")).kwargs["base_url"] == (  # type: ignore[attr-defined]
        "https://api.acme.example/v1"
    )
    assert create(ModelConfig("acme", "m", base_url="http://gw.permitido.example/v1")).kwargs["base_url"] == (  # type: ignore[attr-defined]
        "http://gw.permitido.example/v1"
    )
    with pytest.raises(InvalidModelConfigError, match="MODEL_BASE_URL_ALLOWLIST"):
        create(ModelConfig("acme", "m", base_url="https://fora.example/v1"))
    with pytest.raises(InvalidModelConfigError, match="metadata"):
        create(ModelConfig("acme", "m", base_url="http://169.254.169.254/latest"))
    with pytest.raises(InvalidModelConfigError, match="https"):
        create(ModelConfig("acme", "m", base_url="http://api.acme.example/v1"))  # host do provider: só https


@pytest.mark.usefixtures("acme")
def test_plugin_passa_pela_mesma_regra_de_chave(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("ACME_API_KEY", raising=False)
    (tmp_path / "acme").write_text("chave-do-arquivo\n", encoding="utf-8")
    registry = _registry(secrets_dir=str(tmp_path))
    load_provider_plugins(registry, allowlist=("acme-plugin:acme",), logger=RecordingLogger())

    with pytest.raises(InvalidModelConfigError, match="ACME_API_KEY não configurado"):
        registry.create_model(ModelConfig("acme", "m"))
    model = registry.create_model(ModelConfig("acme", "m", api_key_ref=f"file:{tmp_path}/acme"))
    assert model.kwargs["api_key"] == "chave-do-arquivo"  # type: ignore[attr-defined]
    with pytest.raises(InvalidModelConfigError, match="model_params"):
        registry.create_model(ModelConfig("acme", "m", params={"top_k": 3}, api_key_ref=f"file:{tmp_path}/acme"))


# ── duplicatas: erro de startup com mensagem clara ─────────────────────


@pytest.mark.parametrize(
    ("provider_id", "aliases", "clash"),
    [("openai", (), "openai"), ("acme", ("Google",), "google"), ("OLLAMA", (), "ollama")],
)
def test_conflito_com_built_in_por_id_ou_alias(
    plugin_site: FakeSite, provider_id: str, aliases: tuple[str, ...], clash: str
) -> None:
    module = spec_module(provider_id, *aliases)
    plugin_site.install("acme-plugin", {"acme": "acme_dup_mod:SPEC"}, {"acme_dup_mod": module})
    registry = _registry()

    with pytest.raises(PluginLoadError) as caught:
        load_provider_plugins(registry, allowlist=("acme-plugin:acme",), logger=RecordingLogger())

    assert str(caught.value) == (
        f"plugin 'acme-plugin:acme': provider '{provider_id}' com id ou alias já registrado "
        f"({clash}: built-in); id e aliases de provider são únicos, sem diferenciar caixa"
    )


@pytest.mark.parametrize(
    ("second_id", "second_aliases", "clash"), [("acme", (), "acme"), ("beta", ("ACME-AI",), "acme-ai")]
)
def test_conflito_entre_plugins_por_id_ou_alias(
    plugin_site: FakeSite, second_id: str, second_aliases: tuple[str, ...], clash: str
) -> None:
    plugin_site.install("acme-plugin", {"acme": "acme_one_mod:SPEC"}, {"acme_one_mod": spec_module("acme", "acme-ai")})
    second = spec_module(second_id, *second_aliases)
    plugin_site.install("beta-plugin", {"beta": "beta_one_mod:SPEC"}, {"beta_one_mod": second})

    with pytest.raises(PluginLoadError) as caught:
        load_provider_plugins(_registry(), allowlist=("acme-plugin:acme", "beta-plugin:beta"), logger=RecordingLogger())

    # Na mesma pasta do sys.path a ordem de descoberta é a do sistema de arquivos: a mensagem
    # cita quem chegou depois e, no conflito, quem já estava registrado.
    ids = {"acme-plugin:acme": "acme", "beta-plugin:beta": second_id}
    expected = {
        f"plugin '{late}': provider '{ids[late]}' com id ou alias já registrado ({clash}: plugin '{early}'); "
        "id e aliases de provider são únicos, sem diferenciar caixa"
        for late, early in (("acme-plugin:acme", "beta-plugin:beta"), ("beta-plugin:beta", "acme-plugin:acme"))
    }
    assert str(caught.value) in expected


def test_id_repetido_na_propria_spec(plugin_site: FakeSite) -> None:
    plugin_site.install("acme-plugin", {"acme": "acme_self_mod:SPEC"}, {"acme_self_mod": spec_module("acme", "ACME")})

    with pytest.raises(PluginLoadError) as caught:
        load_provider_plugins(_registry(), allowlist=("acme-plugin:acme",), logger=RecordingLogger())

    # Mensagem do próprio ``ProviderRegistry.register`` (a regra fica num lugar só).
    assert str(caught.value) == (
        "plugin 'acme-plugin:acme': Provider 'acme': id ou alias duplicado (acme, acme); "
        "id e aliases de provider são únicos, sem diferenciar caixa"
    )


_MALFORMED = """
from src.infrastructure.providers import ClassSpec, ProviderSpec

SPEC = ProviderSpec(id={provider_id}, sdk_package="x", aliases={aliases}, chat=ClassSpec(class_path="x.Y"))
"""


@pytest.mark.parametrize(
    ("provider_id", "aliases"),
    [
        ("None", "()"),
        ("''", "()"),
        ("'   '", "()"),
        ("1", "()"),
        ("'zz'", "'ab'"),
        ("'zz'", "('a', 1)"),
        ("'zz'", "['a']"),
    ],
    ids=["id-none", "id-vazio", "id-espacos", "id-int", "aliases-str", "alias-int", "aliases-lista"],
)
def test_provider_spec_malformada_recusa_com_a_origem(plugin_site: FakeSite, provider_id: str, aliases: str) -> None:
    """R3: ``id=None`` virava ``AttributeError`` cru e ``aliases="ab"`` registrava ``a`` e ``b`` em silêncio."""
    source = _MALFORMED.format(provider_id=provider_id, aliases=aliases)
    plugin_site.install("acme-plugin", {"acme": "acme_malformed_mod:SPEC"}, {"acme_malformed_mod": source})
    registry = _registry()
    before = registry.supported("chat")

    with pytest.raises(PluginLoadError) as caught:
        load_provider_plugins(registry, allowlist=("acme-plugin:acme",), logger=RecordingLogger())

    assert str(caught.value) == "plugin 'acme-plugin:acme': ProviderSpec inválida (id/aliases)"
    assert registry.supported("chat") == before


# ── carga do plugin ────────────────────────────────────────────────────


def test_entry_point_que_nao_e_provider_spec_recusa(plugin_site: FakeSite) -> None:
    plugin_site.install("acme-plugin", {"acme": "acme_wrong_mod:SPEC"}, {"acme_wrong_mod": "SPEC = {'id': 'acme'}\n"})

    with pytest.raises(PluginLoadError) as caught:
        load_provider_plugins(_registry(), allowlist=("acme-plugin:acme",), logger=RecordingLogger())

    assert str(caught.value) == (
        "plugin 'acme-plugin:acme': o entry point deve apontar para uma ProviderSpec (recebido: dict)"
    )


def test_falha_ao_importar_plugin_recusa_com_o_tipo_do_erro(plugin_site: FakeSite) -> None:
    plugin_site.install("acme-plugin", {"acme": "acme_inexistente_mod:SPEC"})

    with pytest.raises(PluginLoadError) as caught:
        load_provider_plugins(_registry(), allowlist=("acme-plugin:acme",), logger=RecordingLogger())

    assert str(caught.value) == "plugin 'acme-plugin:acme': falha ao carregar (ModuleNotFoundError)"
    assert isinstance(caught.value.__cause__, ModuleNotFoundError)  # traceback preservado para o operador


def test_entry_point_de_outro_grupo_e_ignorado(plugin_site: FakeSite) -> None:
    plugin_site.install(
        "acme-plugin",
        {"acme": "acme_other_group_mod:SPEC"},
        {"acme_other_group_mod": spec_module("acme")},
        group="outro.grupo",
    )
    logger = RecordingLogger()

    load_provider_plugins(_registry(), allowlist=(), logger=logger)

    assert logger.records == []
    assert not plugin_site.imported("acme_other_group_mod")


def test_sem_allowlist_e_sem_plugins_nada_muda() -> None:
    registry = _registry()
    before = registry.supported("chat")
    logger = RecordingLogger()

    load_provider_plugins(registry, allowlist=(), logger=logger)

    assert registry.supported("chat") == before
    assert logger.records == []


# ── import dinâmico (módulo:atributo) ─────────────────────────────────


def test_import_dinamico_permitido_registra_e_audita(plugin_site: FakeSite, monkeypatch: pytest.MonkeyPatch) -> None:
    plugin_site.install("dyn-holder", {}, {"dyn_ok_mod": spec_module("dyn")})
    monkeypatch.setenv("ACME_API_KEY", ACME_KEY)
    registry = _registry()
    logger = RecordingLogger()

    load_provider_plugins(
        registry, allowlist=(), logger=logger, dynamic_specs=("dyn_ok_mod:SPEC",), dynamic_import_allowed=True
    )

    model = registry.create_model(ModelConfig("dyn", "m"))
    assert isinstance(model, RecordingChatModel)
    assert model.kwargs == {"id": "m", "api_key": ACME_KEY}
    assert [(r.level, r.message, r.context) for r in logger.records] == [
        (
            "warning",
            "Provider carregado por import dinâmico (DYNAMIC_PROVIDER_SPECS, só no modo dev local)",
            {"entry": 1, "target": "dyn_ok_mod:SPEC", "provider_id": "dyn"},
        )
    ]


def test_import_dinamico_conflitando_com_plugin(plugin_site: FakeSite) -> None:
    plugin_site.install("acme-plugin", {"acme": "acme_dyn_clash_mod:SPEC"}, {"acme_dyn_clash_mod": spec_module("acme")})
    plugin_site.install("dyn-holder", {}, {"dyn_clash_mod": spec_module("ACME")})

    with pytest.raises(PluginLoadError) as caught:
        load_provider_plugins(
            _registry(),
            allowlist=("acme-plugin:acme",),
            logger=RecordingLogger(),
            dynamic_specs=("dyn_clash_mod:SPEC",),
            dynamic_import_allowed=True,
        )

    assert str(caught.value) == (
        "DYNAMIC_PROVIDER_SPECS (entrada 1): provider 'ACME' com id ou alias já registrado "
        "(acme: plugin 'acme-plugin:acme'); id e aliases de provider são únicos, sem diferenciar caixa"
    )


def test_import_dinamico_que_falha_cita_so_a_posicao(plugin_site: FakeSite) -> None:
    with pytest.raises(PluginLoadError) as caught:
        load_provider_plugins(
            _registry(),
            allowlist=(),
            logger=RecordingLogger(),
            dynamic_specs=("modulo_que_nao_existe_f203:SPEC",),
            dynamic_import_allowed=True,
        )

    assert str(caught.value) == "DYNAMIC_PROVIDER_SPECS (entrada 1): falha ao carregar (ModuleNotFoundError)"
