"""F2-03: o exemplo ``examples/provider_plugin`` funciona como documentado.

Sem ``pip install``: o ``dist-info`` falso usa o nome, a versão e o entry point declarados no
``pyproject.toml`` do exemplo, e o pacote vem de ``examples/provider_plugin/src``. Se o exemplo
(ou a API de ``ProviderSpec``) mudar, este teste quebra antes da documentação ficar errada.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from agno.models.deepseek import DeepSeek

from src.domain.entities.model_config import ModelConfig
from src.domain.ports.model_factory_port import InvalidModelConfigError
from src.infrastructure.providers import ProviderRegistry
from src.infrastructure.providers.builtins import BUILTIN_PROVIDERS
from src.infrastructure.providers.plugins import ENTRY_POINT_GROUP, load_provider_plugins
from tests.fakes import RecordingLogger
from tests.fakes.plugins import FakeSite

EXAMPLE = Path(__file__).resolve().parents[2] / "examples" / "provider_plugin"
EXAMPLE_KEY = "chave-deepseek-de-teste"


@pytest.fixture
def example_registry(plugin_site: FakeSite, monkeypatch: pytest.MonkeyPatch) -> ProviderRegistry:
    project = tomllib.loads((EXAMPLE / "pyproject.toml").read_text(encoding="utf-8"))["project"]
    entry_points = project["entry-points"][ENTRY_POINT_GROUP]
    plugin_site.install(project["name"], entry_points, version=project["version"])
    monkeypatch.syspath_prepend(str(EXAMPLE / "src"))
    plugin_site.modules.append("orquestrador_provider_deepseek")  # o teardown tira do sys.modules
    registry = ProviderRegistry(BUILTIN_PROVIDERS)
    load_provider_plugins(registry, allowlist=("orquestrador-provider-deepseek:deepseek",), logger=RecordingLogger())
    return registry


def test_exemplo_registra_deepseek_e_cria_o_modelo_real_do_agno(
    example_registry: ProviderRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", EXAMPLE_KEY)

    model = example_registry.create_model(ModelConfig("DeepSeek", "deepseek-chat", params={"temperature": 0.1}))

    assert isinstance(model, DeepSeek)
    assert (model.id, model.base_url, model.api_key, model.temperature) == (
        "deepseek-chat",
        "https://api.deepseek.com",
        EXAMPLE_KEY,
        0.1,
    )


def test_exemplo_nao_ganha_atalho_de_destino(
    example_registry: ProviderRegistry, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("DEEPSEEK_API_KEY", EXAMPLE_KEY)

    with pytest.raises(InvalidModelConfigError, match="https"):
        example_registry.create_model(ModelConfig("deepseek", "deepseek-chat", base_url="http://api.deepseek.com"))
    with pytest.raises(InvalidModelConfigError, match="não suportado para embedder"):
        example_registry.create_embedder(ModelConfig("deepseek", "x"))
