"""QA F2-03: entradas hostis no loader de plugins (metadados, alvo do entry point, destino).

Complementa ``test_provider_plugin_allowlist.py``: nome de distribuição forjado ou ambíguo,
``METADATA`` ausente ou ilegível, entry point que não é ``ProviderSpec``, import que levanta com
texto sensível e plugin que mira destino proibido. Distribuições falsas de ``tests/fakes/plugins.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from src.domain.entities.model_config import ModelConfig
from src.domain.ports.model_factory_port import InvalidModelConfigError
from src.infrastructure.providers import ProviderRegistry
from src.infrastructure.providers.builtins import BUILTIN_PROVIDERS
from src.infrastructure.providers.plugins import PluginLoadError, load_provider_plugins
from tests.fakes import RecordingLogger
from tests.fakes.plugins import FakeSite, spec_module

SECRET = "SEGREDO-QA-F203-NO-TEXTO-DO-PLUGIN"  # noqa: S105 - marcador de vazamento
ALLOW = ("acme-plugin:acme",)


def _load(allowlist: tuple[str, ...] = ALLOW, **kwargs: Any) -> tuple[ProviderRegistry, RecordingLogger]:
    registry = ProviderRegistry(BUILTIN_PROVIDERS)
    logger = RecordingLogger()
    load_provider_plugins(registry, allowlist=allowlist, logger=logger, **kwargs)
    return registry, logger


def _raw_dist(site: FakeSite, folder: str, entry_point: str, metadata: bytes | None) -> None:
    """``*.dist-info`` escrito à mão (METADATA ausente ou com bytes arbitrários)."""
    info = site.root / folder
    info.mkdir()
    (info / "entry_points.txt").write_text(f"[orquestrador.providers]\n{entry_point}\n", encoding="utf-8")
    if metadata is not None:
        (info / "METADATA").write_bytes(metadata)


# ── nome da distribuição forjado ou ambíguo ──────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "forged_name",
    [
        "acme-plugin:acme",  # tenta embutir o separador da allowlist
        "acme plugin",
        "acme-plugin-",
        "acme-pluginx",
        "acme–plugin",  # noqa: RUF001 - travessão (en dash) de propósito
        "\u0430cme-plugin",  # 'a' cirílico
        "acme-plugin​",  # espaço de largura zero
        "acme-plugin/../x",
    ],
    ids=["separador", "espaco", "hifen-final", "sufixo", "en-dash", "cirilico", "zero-width", "barra"],
)
def test_nome_forjado_nao_casa_com_a_allowlist_e_nunca_e_importado(plugin_site: FakeSite, forged_name: str) -> None:
    info = plugin_site.root / "forjado-1.0.dist-info"
    info.mkdir()
    (info / "METADATA").write_text(f"Metadata-Version: 2.1\nName: {forged_name}\nVersion: 1.0\n", encoding="utf-8")
    (info / "entry_points.txt").write_text("[orquestrador.providers]\nacme = forjado_mod:SPEC\n", encoding="utf-8")
    (plugin_site.root / "forjado_mod.py").write_text(plugin_site.side_effect_module("forjado_mod"), encoding="utf-8")
    plugin_site.modules.append("forjado_mod")

    with pytest.raises(PluginLoadError, match="PLUGIN_ALLOWLIST"):
        _load()

    assert not plugin_site.imported("forjado_mod")


@pytest.mark.parametrize("name", ["ACME_PLUGIN", "acme.plugin", "  Acme--Plugin  ", "Acme_._Plugin"])
def test_variantes_pep_503_do_mesmo_nome_casam(plugin_site: FakeSite, name: str) -> None:
    plugin_site.install(name.strip(), {"acme": "acme_pep_mod:SPEC"}, {"acme_pep_mod": spec_module("acme")})

    registry, _ = _load()

    assert "acme" in registry.supported("chat")


def test_nome_do_entry_point_e_comparado_exato_sem_ignorar_caixa(plugin_site: FakeSite) -> None:
    module = plugin_site.side_effect_module("acme_case_mod")
    plugin_site.install("acme-plugin", {"acme": "acme_case_mod:SPEC"}, {"acme_case_mod": module})

    with pytest.raises(PluginLoadError, match="PLUGIN_ALLOWLIST"):
        _load(("acme-plugin:ACME",))

    assert not plugin_site.imported("acme_case_mod")


def test_dois_cabecalhos_name_valem_o_primeiro_como_o_pip(plugin_site: FakeSite) -> None:
    """Ambiguidade no METADATA: o primeiro ``Name`` decide, e quem veio depois não "libera" o plugin."""
    _raw_dist(
        plugin_site, "dup_name-1.0.dist-info", "acme = dup_name_mod:SPEC",
        b"Metadata-Version: 2.1\nName: evil-plugin\nName: acme-plugin\nVersion: 1.0\n",
    )
    (plugin_site.root / "dup_name_mod.py").write_text(plugin_site.side_effect_module("dup_name_mod"), encoding="utf-8")
    plugin_site.modules.append("dup_name_mod")

    with pytest.raises(PluginLoadError, match="PLUGIN_ALLOWLIST"):
        _load()

    assert not plugin_site.imported("dup_name_mod")


def test_allowlist_repetida_carrega_uma_vez_sem_erro(plugin_site: FakeSite) -> None:
    plugin_site.install("acme-plugin", {"acme": "acme_twice_mod:SPEC"}, {"acme_twice_mod": spec_module("acme")})

    registry, logger = _load(("acme-plugin:acme", "acme-plugin:acme"))

    assert "acme" in registry.supported("chat")
    assert logger.messages("info") == ["Plugin de provider carregado"]


# ── METADATA ausente ou ilegível: uma distribuição quebrada não derruba nem libera nada ──────────


@pytest.mark.parametrize(
    "metadata",
    [
        None,
        b"",
        b"\xff\xfe\x00 lixo binario\n",  # BUG-F2-03-QA-1: escapava como UnicodeDecodeError
        b"Name:\nVersion: 1\n",
        b"Name:    \n",
    ],
    ids=["sem-arquivo", "vazio", "binario", "name-vazio", "name-so-espacos"],
)
def test_distribuicao_sem_metadata_utilizavel_e_recusada_sem_quebrar_o_loader(
    plugin_site: FakeSite, metadata: bytes | None
) -> None:
    _raw_dist(plugin_site, "quebrada-1.0.dist-info", "quebrada = quebrada_mod:SPEC", metadata)
    (plugin_site.root / "quebrada_mod.py").write_text(plugin_site.side_effect_module("quebrada_mod"), encoding="utf-8")
    plugin_site.modules.append("quebrada_mod")
    plugin_site.install("acme-plugin", {"acme": "acme_ok2_mod:SPEC"}, {"acme_ok2_mod": spec_module("acme")})

    registry, logger = _load()  # a distribuição boa continua carregando

    assert "acme" in registry.supported("chat")
    assert not plugin_site.imported("quebrada_mod")
    refused = [r for r in logger.records if r.level == "warning"]
    assert [r.context for r in refused] == [{"entry_point": "quebrada"}]


# ── alvo do entry point ──────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("source", "received"),
    [
        ("SPEC = None\n", "NoneType"),
        ("SPEC = 'openai'\n", "str"),
        ("SPEC = lambda: None\n", "function"),
        ("from src.infrastructure.providers import ProviderSpec\nSPEC = ProviderSpec\n", "type"),
        ("from src.infrastructure.providers import ProviderSpec\nSPEC = [ProviderSpec('x', 'y')]\n", "list"),
        ("class SPEC:\n    id = 'acme'\n", "type"),
    ],
    ids=["none", "str", "funcao", "a-classe-ProviderSpec", "lista-de-specs", "classe-qualquer"],
)
def test_entry_point_que_nao_e_uma_provider_spec_recusa_com_o_tipo(
    plugin_site: FakeSite, source: str, received: str
) -> None:
    plugin_site.install("acme-plugin", {"acme": "acme_kind_mod:SPEC"}, {"acme_kind_mod": source})

    with pytest.raises(PluginLoadError) as caught:
        _load()

    assert str(caught.value) == (
        f"plugin 'acme-plugin:acme': o entry point deve apontar para uma ProviderSpec (recebido: {received})"
    )


def test_entry_point_sem_o_atributo_recusa_com_o_tipo_do_erro(plugin_site: FakeSite) -> None:
    plugin_site.install("acme-plugin", {"acme": "acme_noattr_mod:FALTA"}, {"acme_noattr_mod": "SPEC = 1\n"})

    with pytest.raises(PluginLoadError) as caught:
        _load()

    assert str(caught.value) == "plugin 'acme-plugin:acme': falha ao carregar (AttributeError)"


@pytest.mark.parametrize(
    "exception",
    ["RuntimeError", "SystemExit", "KeyboardInterrupt", "ImportError"],
)
def test_import_do_plugin_que_levanta_com_texto_sensivel_nao_vaza_na_mensagem(
    plugin_site: FakeSite, exception: str
) -> None:
    plugin_site.install(
        "acme-plugin", {"acme": "acme_boom_mod:SPEC"}, {"acme_boom_mod": f"raise {exception}({SECRET!r})\n"}
    )

    if exception in ("SystemExit", "KeyboardInterrupt"):
        # BaseException não é capturada de propósito: o processo deve sair, não seguir sem o plugin.
        with pytest.raises(BaseException) as caught:
            _load()
        assert type(caught.value).__name__ == exception
        return
    with pytest.raises(PluginLoadError) as caught:
        _load()
    assert str(caught.value) == f"plugin 'acme-plugin:acme': falha ao carregar ({exception})"
    assert SECRET not in str(caught.value)


def test_import_dinamico_de_objeto_que_carrega_env_nao_vaza_o_valor(
    plugin_site: FakeSite, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("QA_F203_VAR_API_KEY", SECRET)

    with pytest.raises(PluginLoadError) as caught:
        _load((), dynamic_specs=("os:environ",), dynamic_import_allowed=True)

    assert str(caught.value) == (
        "DYNAMIC_PROVIDER_SPECS (entrada 1): o entry point deve apontar para uma ProviderSpec (recebido: _Environ)"
    )
    assert SECRET not in str(caught.value)


# ── plugin sob a guarda de destino do F2-02 ──────────────────────────────────────────────────────


def test_plugin_com_host_de_metadata_como_host_do_provider_continua_recusado(plugin_site: FakeSite) -> None:
    """O ``default_hosts`` vem do plugin (código liberado), mas metadata/link-local é recusado sempre."""
    module = spec_module("acme").replace('{"api.acme.example"}', '{"169.254.169.254"}')
    plugin_site.install("acme-plugin", {"acme": "acme_meta_mod:SPEC"}, {"acme_meta_mod": module})
    registry, _ = _load()

    with pytest.raises(InvalidModelConfigError, match="metadata") as caught:
        registry.create_model(ModelConfig("acme", "m", base_url="https://169.254.169.254/latest"))

    assert "169.254.169.254" not in str(caught.value)
