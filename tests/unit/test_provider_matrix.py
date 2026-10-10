"""Matriz de providers (F1-06, B1; F2-02): cada provider do registry instancia ou falha nomeando o pacote.

Sem rede: os construtores do agno 2.5.8 não abrem conexão (o cliente do SDK nasce no
primeiro uso). Os SDKs opcionais que não estão instalados (``anthropic``, ``groq``; ver
``requirements.in``) são trocados por um stub de import só durante o teste, para provar
que o caminho/classe da spec existe no agno instalado: foi assim que o B1 passou
despercebido (``agno.models.groq.chat``/``GroqChat`` não existem; o ``ImportError`` do
caminho errado virava "instale groq").

A ausência de SDK é simulada com ``sys.modules[<sdk>] = None`` (o import levanta
``ImportError``) e os módulos do provider no agno saem do cache para serem reimportados.
"""

from __future__ import annotations

import sys

import pytest

from src.domain.entities.model_config import ModelConfig
from src.domain.ports.model_factory_port import InvalidModelConfigError
from src.infrastructure.providers import DestinationPolicy, ProviderRegistry
from src.infrastructure.providers.builtins import BUILTIN_PROVIDERS

API_KEY = "chave-de-teste-sem-valor"  # nunca chega a um servidor: nada faz request
COMPAT_URL = "https://llm.compat.example/v1"

# provider -> (classe esperada, SDK do qual ela depende, pacote que a mensagem manda instalar)
MODEL_MATRIX: dict[str, tuple[str, str | None, str | None]] = {
    "ollama": ("agno.models.ollama.chat.Ollama", None, None),  # dependência obrigatória
    "openai": ("agno.models.openai.chat.OpenAIChat", "openai", "openai"),
    "anthropic": ("agno.models.anthropic.claude.Claude", "anthropic", "anthropic"),
    "gemini": ("agno.models.google.gemini.Gemini", "google.genai", "google-genai"),
    "groq": ("agno.models.groq.groq.Groq", "groq", "groq"),
    "azure": ("agno.models.azure.openai_chat.AzureOpenAI", "openai", "openai"),
    "openai_compatible": ("agno.models.openai.like.OpenAILike", "openai", "openai"),
}
MODEL_ALIASES = {"google": "gemini", "azureopenai": "azure"}

EMBEDDER_MATRIX: dict[str, tuple[str, str, str]] = {
    "ollama": ("agno.knowledge.embedder.ollama.OllamaEmbedder", "ollama", "ollama"),
    "openai": ("agno.knowledge.embedder.openai.OpenAIEmbedder", "openai", "openai"),
    "gemini": ("agno.knowledge.embedder.google.GeminiEmbedder", "google.genai", "google-genai"),
    "azure": ("agno.knowledge.embedder.azure_openai.AzureOpenAIEmbedder", "openai", "openai"),
    "openai_compatible": ("agno.knowledge.embedder.openai_like.OpenAILikeEmbedder", "openai", "openai"),
}
EMBEDDER_ALIASES = {"google": "gemini", "azureopenai": "azure"}

# Classe que herda de outra do agno: a mãe (já em cache) também sai, senão o SDK nem é importado.
PARENT_MODULES = {
    "agno.models.openai.like": "agno.models.openai.chat",
    "agno.knowledge.embedder.openai_like": "agno.knowledge.embedder.openai",
}


def _registry() -> ProviderRegistry:
    """Built-ins com a allowlist do ``openai_compatible`` e DNS falso (nada resolve de verdade)."""
    policy = DestinationPolicy(allowlist=("llm.compat.example",), resolver=lambda host: ["10.0.0.9"])
    return ProviderRegistry(BUILTIN_PROVIDERS, policy=policy)


def _config(provider: str, model_id: str) -> ModelConfig:
    base_url = COMPAT_URL if provider == "openai_compatible" else None
    return ModelConfig(provider, model_id, base_url=base_url)


def _qualname(obj: object) -> str:
    cls = type(obj)
    return f"{cls.__module__}.{cls.__qualname__}"


def _module_of(class_path: str) -> str:
    return class_path.rsplit(".", 1)[0]


def _drop_cached(monkeypatch: pytest.MonkeyPatch, prefix: str) -> None:
    """Tira ``prefix`` (e a classe mãe), com submódulos, do cache; o monkeypatch devolve tudo no teardown."""
    prefixes = [prefix, *([PARENT_MODULES[prefix]] if prefix in PARENT_MODULES else [])]
    for name in [n for n in sys.modules if any(n == p or n.startswith(p + ".") for p in prefixes)]:
        monkeypatch.delitem(sys.modules, name)


@pytest.fixture
def provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Chave de cada provider no ambiente (como antes); Azure exige ``AZURE_ENDPOINT``."""
    for name in ("OPENAI", "ANTHROPIC", "GEMINI", "GROQ", "AZURE"):
        monkeypatch.setenv(f"{name}_API_KEY", API_KEY)
    monkeypatch.setenv("AZURE_ENDPOINT", "https://azure.example.invalid")
    monkeypatch.setenv("AZURE_VERSION", "2024-10-21")


# ── modelos de chat ──────────────────────────────────────────────────


def test_matriz_cobre_todos_os_providers_suportados() -> None:
    registry = _registry()
    assert set(MODEL_MATRIX) | set(MODEL_ALIASES) == set(registry.supported("chat"))
    assert set(EMBEDDER_MATRIX) | set(EMBEDDER_ALIASES) == set(registry.supported("embedder"))


@pytest.mark.usefixtures("optional_sdks_stubbed", "provider_env")
@pytest.mark.parametrize("provider", sorted(MODEL_MATRIX) + sorted(MODEL_ALIASES))
def test_cada_provider_de_modelo_instancia_a_classe_real_do_agno(provider: str) -> None:
    expected, _, _ = MODEL_MATRIX[MODEL_ALIASES.get(provider, provider)]

    model = _registry().create_model(_config(provider, "modelo-x"))

    assert _qualname(model) == expected
    assert model.id == "modelo-x"


@pytest.mark.usefixtures("optional_sdks_stubbed", "provider_env")
@pytest.mark.parametrize("provider", ["openai", "anthropic", "gemini", "groq", "azure"])
def test_chave_do_ambiente_chega_ao_modelo(provider: str) -> None:
    """Sem campos novos, cada provider recebe a ``<PROVEDOR>_API_KEY`` do ambiente, como antes."""
    model = _registry().create_model(_config(provider, "modelo-x"))

    assert model.api_key == API_KEY  # type: ignore[attr-defined]


@pytest.mark.usefixtures("provider_env")
@pytest.mark.parametrize(
    "provider", sorted(p for p, (_, sdk, _) in MODEL_MATRIX.items() if sdk is not None)
)
def test_provider_de_modelo_sem_sdk_falha_nomeando_o_pacote(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class_path, sdk, package = MODEL_MATRIX[provider]
    assert sdk is not None and package is not None
    monkeypatch.setitem(sys.modules, sdk, None)
    _drop_cached(monkeypatch, _module_of(class_path))

    with pytest.raises(InvalidModelConfigError) as caught:
        _registry().create_model(_config(provider, "modelo-x"))

    message = str(caught.value)
    assert f"pip install {package}" in message
    assert "\n" not in message and "Traceback" not in message


# ── embedders ────────────────────────────────────────────────────────


@pytest.mark.usefixtures("provider_env")
@pytest.mark.parametrize("provider", sorted(EMBEDDER_MATRIX) + sorted(EMBEDDER_ALIASES))
def test_cada_provider_de_embedder_instancia_a_classe_real_do_agno(provider: str) -> None:
    expected, _, _ = EMBEDDER_MATRIX[EMBEDDER_ALIASES.get(provider, provider)]

    embedder = _registry().create_embedder(_config(provider, "emb-x"))

    assert _qualname(embedder) == expected
    assert embedder.id == "emb-x"  # type: ignore[attr-defined]


@pytest.mark.usefixtures("provider_env")
@pytest.mark.parametrize("provider", sorted(EMBEDDER_MATRIX))
def test_provider_de_embedder_sem_sdk_falha_nomeando_o_pacote(
    provider: str, monkeypatch: pytest.MonkeyPatch
) -> None:
    class_path, sdk, package = EMBEDDER_MATRIX[provider]
    monkeypatch.setitem(sys.modules, sdk, None)
    _drop_cached(monkeypatch, _module_of(class_path))

    with pytest.raises(InvalidModelConfigError) as caught:
        _registry().create_embedder(_config(provider, "emb-x"))

    message = str(caught.value)
    assert f"pip install {package}" in message
    assert "\n" not in message and "Traceback" not in message
