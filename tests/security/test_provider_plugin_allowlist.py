"""F2-03: plugin de provider fora da ``PLUGIN_ALLOWLIST`` nunca é importado.

A verificação usa só metadados (distribuição + nome do entry point) antes de ``ep.load()``.
Prova de "nunca importado": o módulo do plugin grava um marcador ao ser importado, e o teste
confere que o marcador não existe e que o módulo não está em ``sys.modules``. As distribuições
são falsas (``tests/fakes/plugins.py``) e descobertas pelo ``importlib.metadata`` real.
"""

from __future__ import annotations

import pytest

from src.infrastructure.providers import ProviderRegistry
from src.infrastructure.providers.builtins import BUILTIN_PROVIDERS
from src.infrastructure.providers.plugins import PluginLoadError, load_provider_plugins
from tests.fakes import RecordingLogger
from tests.fakes.plugins import FakeSite


def _load(allowlist: tuple[str, ...], **kwargs: object) -> tuple[ProviderRegistry, RecordingLogger]:
    registry = ProviderRegistry(BUILTIN_PROVIDERS)
    logger = RecordingLogger()
    load_provider_plugins(registry, allowlist=allowlist, logger=logger, **kwargs)  # type: ignore[arg-type]
    return registry, logger


def test_plugin_fora_da_allowlist_nunca_e_importado(plugin_site: FakeSite) -> None:
    module = plugin_site.side_effect_module("acme_plugin_mod")
    plugin_site.install("acme-plugin", {"acme": "acme_plugin_mod:SPEC"}, {"acme_plugin_mod": module})

    registry, logger = _load(())

    assert not plugin_site.imported("acme_plugin_mod")
    assert "acme" not in registry.supported("chat")
    assert [(r.level, r.message, r.context) for r in logger.records] == [
        (
            "warning",
            "Plugin de provider ignorado: fora da PLUGIN_ALLOWLIST",
            {"distribution": "acme-plugin", "entry_point": "acme"},
        )
    ]


def test_allowlist_vale_por_entry_point_e_nao_pela_distribuicao_inteira(plugin_site: FakeSite) -> None:
    plugin_site.install(
        "acme-plugin",
        {"acme": "acme_ok_mod:SPEC", "extra": "acme_extra_mod:SPEC"},
        {
            "acme_ok_mod": plugin_site.side_effect_module("acme_ok_mod", "acme"),
            "acme_extra_mod": plugin_site.side_effect_module("acme_extra_mod", "extra"),
        },
    )

    registry, _ = _load(("acme-plugin:acme",))

    assert plugin_site.imported("acme_ok_mod")
    assert not plugin_site.imported("acme_extra_mod")
    assert "acme" in registry.supported("chat")
    assert "extra" not in registry.supported("chat")


def test_mesmo_nome_de_entry_point_em_outra_distribuicao_nao_passa(plugin_site: FakeSite) -> None:
    """A allowlist casa distribuição E nome: outra distribuição com o nome permitido não é importada."""
    module = plugin_site.side_effect_module("evil_mod")
    plugin_site.install("evil-plugin", {"acme": "evil_mod:SPEC"}, {"evil_mod": module})

    registry = ProviderRegistry(BUILTIN_PROVIDERS)
    logger = RecordingLogger()
    with pytest.raises(PluginLoadError, match="PLUGIN_ALLOWLIST"):
        load_provider_plugins(registry, allowlist=("acme-plugin:acme",), logger=logger)

    assert not plugin_site.imported("evil_mod")
    # Controle positivo: a evil-plugin foi descoberta e recusada pela allowlist, não ignorada por acaso.
    assert [(r.level, r.message, r.context) for r in logger.records] == [
        (
            "warning",
            "Plugin de provider ignorado: fora da PLUGIN_ALLOWLIST",
            {"distribution": "evil-plugin", "entry_point": "acme"},
        )
    ]


def test_duas_distribuicoes_com_o_mesmo_name_recusam_antes_de_importar(plugin_site: FakeSite) -> None:
    """R1: o stdlib deduplica pelo nome do diretório ``*.dist-info``, não pelo ``Name``. Uma entrada
    da allowlist casada por duas distribuições é ambígua: nenhuma é importada."""
    plugin_site.install(
        "acme-plugin",
        {"acme": "spoof_evil_mod:SPEC"},
        {"spoof_evil_mod": plugin_site.side_effect_module("spoof_evil_mod", "evil")},
        folder="aaa_evil-1.0.dist-info",
    )
    plugin_site.install(
        "acme-plugin",
        {"acme": "spoof_good_mod:SPEC"},
        {"spoof_good_mod": plugin_site.side_effect_module("spoof_good_mod", "acme")},
    )

    with pytest.raises(PluginLoadError) as caught:
        _load(("acme-plugin:acme",))

    assert not plugin_site.imported("spoof_evil_mod")
    assert not plugin_site.imported("spoof_good_mod")
    assert str(caught.value) == (
        "PLUGIN_ALLOWLIST: entrada 1 casa com mais de uma distribuição instalada (mesmo Name nos "
        "metadados, em diretórios *.dist-info diferentes); remova a cópia que não deveria estar instalada"
    )


def test_distribuicao_sem_nome_nos_metadados_e_recusada_mesmo_com_o_nome_do_diretorio(plugin_site: FakeSite) -> None:
    """Falha fechada: sem ``Name`` no METADATA não há distribuição identificável (o nome do
    diretório ``anon_plugin-1.0.0.dist-info`` não conta)."""
    plugin_site.install(
        "anon-plugin",
        {"anon": "anon_mod:SPEC"},
        {"anon_mod": plugin_site.side_effect_module("anon_mod", "anon")},
        name_in_metadata=False,
    )
    logger = RecordingLogger()

    with pytest.raises(PluginLoadError, match="PLUGIN_ALLOWLIST"):
        load_provider_plugins(ProviderRegistry(BUILTIN_PROVIDERS), allowlist=("anon-plugin:anon",), logger=logger)

    assert not plugin_site.imported("anon_mod")
    assert [(r.level, r.message, r.context) for r in logger.records] == [
        (
            "warning",
            "Plugin de provider recusado: entry point sem distribuição identificável",
            {"entry_point": "anon"},
        )
    ]


def test_plugin_permitido_ausente_falha_antes_de_importar_qualquer_plugin(plugin_site: FakeSite) -> None:
    """A conferência da allowlist inteira vem antes do primeiro ``ep.load()``."""
    module = plugin_site.side_effect_module("acme_first_mod")
    plugin_site.install("acme-plugin", {"acme": "acme_first_mod:SPEC"}, {"acme_first_mod": module})

    with pytest.raises(PluginLoadError) as caught:
        _load(("acme-plugin:acme", "faltando-plugin:falta"))

    assert not plugin_site.imported("acme_first_mod")
    message = str(caught.value)
    assert "PLUGIN_ALLOWLIST" in message and "entrada 2" in message
    assert "faltando" not in message and "falta" not in message  # cita a posição, nunca o valor da env


def test_import_dinamico_sem_permissao_nunca_importa(plugin_site: FakeSite) -> None:
    plugin_site.install("dyn-holder", {}, {"dyn_mod": plugin_site.side_effect_module("dyn_mod", "dyn")})

    with pytest.raises(PluginLoadError) as caught:
        _load((), dynamic_specs=("dyn_mod:SPEC",), dynamic_import_allowed=False)

    assert not plugin_site.imported("dyn_mod")
    message = str(caught.value)
    assert "DYNAMIC_PROVIDER_SPECS" in message and "ALLOW_DYNAMIC_IMPORT" in message
    assert "dyn_mod" not in message
