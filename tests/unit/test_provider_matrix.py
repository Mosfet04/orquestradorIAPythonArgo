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

import importlib.abc
import importlib.machinery
import importlib.util
import sys
import types
from collections.abc import Iterator

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


# ── stub de SDK opcional ausente ────────────────────────────────────


class _PermissiveModule(types.ModuleType):
    """Módulo que inventa uma classe vazia para qualquer nome importado dele."""

    def __getattr__(self, name: str) -> type:
        if name.startswith("__"):
            raise AttributeError(name)
        stub = type(name, (), {})
        setattr(self, name, stub)
        return stub


class _StubSdkFinder(importlib.abc.MetaPathFinder, importlib.abc.Loader):
    """Responde ``import <raiz>[.x.y]`` com ``_PermissiveModule`` (pacote, aceita submódulos)."""

    def __init__(self, roots: set[str]) -> None:
        self._roots = roots

    def find_spec(
        self, fullname: str, path: object = None, target: object = None
    ) -> importlib.machinery.ModuleSpec | None:
        if fullname.split(".")[0] in self._roots:
            return importlib.util.spec_from_loader(fullname, self, is_package=True)
        return None

    def create_module(self, spec: importlib.machinery.ModuleSpec) -> types.ModuleType:
        return _PermissiveModule(spec.name)

    def exec_module(self, module: types.ModuleType) -> None:
        return None


def _is_installed(sdk: str) -> bool:
    try:
        return importlib.util.find_spec(sdk) is not None
    except ImportError:
        return False


# Módulos do agno que importam o SDK opcional (direto ou via utilitário).
_AGNO_MODULES_OF_SDK = {
    "anthropic": ("agno.models.anthropic", "agno.utils.models.claude"),
    "groq": ("agno.models.groq",),
}


def _forget(prefixes: list[str]) -> None:
    """Tira do cache (e do pacote pai) o que foi importado em cima do stub."""
    for name in [n for n in sys.modules if any(n == p or n.startswith(p + ".") for p in prefixes)]:
        parent, _, child = name.rpartition(".")
        parent_module = sys.modules.get(parent)
        if parent_module is not None and getattr(parent_module, child, None) is sys.modules[name]:
            delattr(parent_module, child)
        del sys.modules[name]


@pytest.fixture
def optional_sdks_stubbed(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Stub dos SDKs opcionais NÃO instalados; com o SDK instalado, vale o real.

    SDK ausente nunca deixa módulo em cache (import falho não fica em ``sys.modules``),
    então antes do teste não há nada a salvar; depois, tudo que veio do stub é esquecido.
    """
    missing = sorted(sdk for sdk in _AGNO_MODULES_OF_SDK if not _is_installed(sdk))
    prefixes = [*missing, *(m for sdk in missing for m in _AGNO_MODULES_OF_SDK[sdk])]
    _forget(prefixes)
    monkeypatch.setattr(sys, "meta_path", [_StubSdkFinder(set(missing)), *sys.meta_path])
    yield
    _forget(prefixes)


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
