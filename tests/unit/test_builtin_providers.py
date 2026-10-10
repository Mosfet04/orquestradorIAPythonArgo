"""Built-ins do ``ProviderRegistry`` (F2-02) nas classes reais do agno 2.5.8, sem rede.

Mantém o comportamento das fábricas antigas sem campos novos (chave ``<PROVEDOR>_API_KEY`` do
ambiente, ``OLLAMA_BASE_URL`` só no Ollama, Azure com ``AZURE_ENDPOINT``) e prova o repasse de
``model_params`` para os kwargs reais. DNS sempre falso.
"""

from __future__ import annotations

import pytest

from src.domain.entities.model_config import ModelConfig
from src.domain.ports import InvalidModelConfigError
from src.infrastructure.providers import DestinationPolicy, ProviderRegistry
from src.infrastructure.providers.builtins import BUILTIN_PROVIDERS

KEY = "chave-de-teste-sem-valor"
OLLAMA_HOST = "http://ollama:11434"


def _registry(*, allowlist: tuple[str, ...] = (), operator: dict[str, str] | None = None) -> ProviderRegistry:
    policy = DestinationPolicy(allowlist=allowlist, resolver=lambda host: ["10.0.0.9"])
    return ProviderRegistry(BUILTIN_PROVIDERS, policy=policy, operator_base_urls=operator)


@pytest.fixture(autouse=True)
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "OPENAI_API_KEY", "GEMINI_API_KEY", "AZURE_API_KEY", "AZURE_ENDPOINT", "AZURE_VERSION", "OLLAMA_API_KEY",
        "OPENAI_BASE_URL",
    ):
        monkeypatch.delenv(name, raising=False)


# ── Ollama e OLLAMA_BASE_URL ─────────────────────────────────────────────────────────────────────


def test_ollama_nao_exige_chave_e_sem_host_mantem_o_default_do_cliente():
    model = _registry().create_model(ModelConfig("ollama", "llama3.2:latest"))

    assert (model.id, model.host) == ("llama3.2:latest", None)  # type: ignore[attr-defined]


def test_ollama_base_url_do_operador_chega_ao_chat_e_ao_embedder():
    registry = _registry(operator={"ollama": OLLAMA_HOST})

    assert registry.create_model(ModelConfig("ollama", "llama3.2")).host == OLLAMA_HOST  # type: ignore[attr-defined]
    assert registry.create_embedder(ModelConfig("ollama", "nomic")).host == OLLAMA_HOST  # type: ignore[attr-defined]


def test_ollama_base_url_nao_vaza_para_outro_provider(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)
    registry = _registry(operator={"ollama": OLLAMA_HOST})

    assert registry.create_model(ModelConfig("openai", "gpt-4o-mini")).base_url is None  # type: ignore[attr-defined]
    assert registry.create_embedder(ModelConfig("openai", "e")).base_url is None  # type: ignore[attr-defined]


def test_base_url_da_config_permitida_prevalece_sobre_a_do_operador_e_desliga_redirect():
    registry = _registry(allowlist=("ollama-gpu.interno",), operator={"ollama": OLLAMA_HOST})
    config = ModelConfig("ollama", "llama3.2", base_url="http://ollama-gpu.interno:11434")

    model = registry.create_model(config)
    embedder = registry.create_embedder(config)

    assert model.host == embedder.host == "http://ollama-gpu.interno:11434"  # type: ignore[attr-defined]
    assert model.get_client()._client.follow_redirects is False  # type: ignore[attr-defined]
    assert embedder.client._client.follow_redirects is False  # type: ignore[attr-defined]


def test_ollama_do_operador_segue_o_default_do_sdk_para_redirect():
    model = _registry(operator={"ollama": OLLAMA_HOST}).create_model(ModelConfig("ollama", "llama3.2"))

    assert model.client_params is None  # type: ignore[attr-defined]


# ── chave do ambiente (comportamento de antes) ───────────────────────────────────────────────────


@pytest.mark.parametrize(("provider", "env"), [("openai", "OPENAI_API_KEY"), ("gemini", "GEMINI_API_KEY"),
                                               ("google", "GEMINI_API_KEY"), ("azure", "AZURE_API_KEY")])
def test_chave_obrigatoria_ausente_cita_a_variavel(provider: str, env: str, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AZURE_ENDPOINT", "https://x.openai.azure.com")

    with pytest.raises(InvalidModelConfigError, match=f"{env} não configurado"):
        _registry().create_model(ModelConfig(provider, "m"))


def test_azure_sem_endpoint_e_erro(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AZURE_API_KEY", KEY)

    with pytest.raises(InvalidModelConfigError, match="AZURE_ENDPOINT não configurado"):
        _registry().create_model(ModelConfig("azureopenai", "gpt-4"))


def test_azure_recebe_chave_endpoint_e_versao_do_ambiente(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AZURE_API_KEY", KEY)
    monkeypatch.setenv("AZURE_ENDPOINT", "https://x.openai.azure.com")
    monkeypatch.setenv("AZURE_VERSION", "2024-02-01")

    model = _registry().create_model(ModelConfig("azure", "gpt-4"))

    assert (model.api_key, model.azure_endpoint, model.api_version) == (  # type: ignore[attr-defined]
        KEY, "https://x.openai.azure.com", "2024-02-01"
    )


def test_embedder_azure_recebe_so_a_chave_como_antes(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("AZURE_API_KEY", KEY)
    monkeypatch.setenv("AZURE_ENDPOINT", "https://x.openai.azure.com")

    embedder = _registry().create_embedder(ModelConfig("azure", "text-embedding-3-small"))

    assert embedder.api_key == KEY  # type: ignore[attr-defined]
    assert embedder.azure_endpoint != "https://x.openai.azure.com"  # type: ignore[attr-defined]


# ── model_params nos kwargs reais ────────────────────────────────────────────────────────────────


def test_model_params_do_ollama_vao_para_options_e_kwargs_de_topo():
    model = _registry().create_model(
        ModelConfig("ollama", "llama3.2", params={"temperature": 0.1, "num_ctx": 8192, "keep_alive": "5m"})
    )

    assert model.options == {"temperature": 0.1, "num_ctx": 8192}  # type: ignore[attr-defined]
    assert model.keep_alive == "5m"  # type: ignore[attr-defined]


def test_model_params_do_openai_viram_atributos(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", KEY)

    model = _registry().create_model(
        ModelConfig("openai", "gpt-4o-mini", params={"temperature": 0.3, "max_tokens": 256, "seed": 7})
    )

    assert (model.temperature, model.max_tokens, model.seed) == (0.3, 256, 7)  # type: ignore[attr-defined]


def test_dimensions_do_embedder_compativel(monkeypatch: pytest.MonkeyPatch):
    embedder = _registry(allowlist=("emb.interno",)).create_embedder(
        ModelConfig("openai_compatible", "bge-m3", params={"dimensions": 1024}, base_url="http://emb.interno/v1")
    )

    assert embedder.dimensions == 1024  # type: ignore[attr-defined]


@pytest.mark.parametrize("provider", ["anthropic", "gemini", "azure"])
def test_providers_sem_base_url_configuravel_recusam_base_url(provider: str):
    with pytest.raises(InvalidModelConfigError, match="não aceita base_url"):
        _registry(allowlist=("gw.interno",)).create_model(ModelConfig(provider, "m", base_url="https://gw.interno"))


def test_openai_compatible_exige_base_url():
    with pytest.raises(InvalidModelConfigError, match="exige base_url"):
        _registry().create_model(ModelConfig("openai_compatible", "m"))
