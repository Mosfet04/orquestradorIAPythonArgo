"""Providers built-in (F2-02): registrados em código, no mesmo ``register`` dos plugins.

Classes e kwargs conferidos no agno 2.5.8 (``agno/models/...``, ``agno/knowledge/embedder/...``).
``params``: allowlist de ``model_params`` por classe (nome do kwarg real; ``options.x`` vai para
o dict ``options`` do Ollama, ver ``ollama/_types.py:Options``). Chave do ambiente como antes da
F2-02: ``<PROVEDOR>_API_KEY`` (gemini lê ``GEMINI_API_KEY``; azure ``AZURE_API_KEY`` +
``AZURE_ENDPOINT``/``AZURE_VERSION``); Ollama sem chave obrigatória.

``openai_compatible`` usa ``OpenAILike``/``OpenAILikeEmbedder``, cujo ``api_key`` padrão é o
placeholder ``"not-provided"``: sem ``api_key_ref``, nada é passado e o SDK ``openai`` não cai
na ``OPENAI_API_KEY`` do ambiente (cairia com ``api_key=None``, e mandaria a chave da OpenAI ao
``base_url`` do documento). Pelo mesmo motivo o embedder não é o ``OpenAIEmbedder``.
"""

from __future__ import annotations

from src.infrastructure.providers.registry import ClassSpec, OperatorEnv, ProviderSpec


def _same(*names: str) -> dict[str, str]:
    return {name: name for name in names}


_OPENAI_CHAT_PARAMS = _same(
    "temperature",
    "top_p",
    "max_tokens",
    "max_completion_tokens",
    "frequency_penalty",
    "presence_penalty",
    "seed",
    "stop",
    "reasoning_effort",
    "timeout",
    "max_retries",
)
_OLLAMA_OPTIONS = (
    "temperature",
    "top_p",
    "top_k",
    "num_ctx",
    "num_predict",
    "seed",
    "repeat_penalty",
    "presence_penalty",
    "frequency_penalty",
)
_EMBEDDER_PARAMS = _same("dimensions")

OLLAMA = ProviderSpec(
    id="ollama",
    sdk_package="ollama",
    chat=ClassSpec(
        class_path="agno.models.ollama.chat.Ollama",
        params={**{name: f"options.{name}" for name in _OLLAMA_OPTIONS}, **_same("keep_alive", "timeout")},
        base_url_kwarg="host",
        untrusted_url_kwargs={"client_params.follow_redirects": False},
    ),
    embedder=ClassSpec(
        class_path="agno.knowledge.embedder.ollama.OllamaEmbedder",
        params=_same("dimensions", "timeout"),
        base_url_kwarg="host",
        api_key_kwarg=None,  # OllamaEmbedder não tem api_key (o SDK lê OLLAMA_API_KEY sozinho)
        untrusted_url_kwargs={"client_kwargs.follow_redirects": False},
    ),
    # Com OLLAMA_API_KEY e sem host, o agno aponta para o Ollama Cloud.
    default_hosts=frozenset({"ollama.com"}),
)

OPENAI = ProviderSpec(
    id="openai",
    sdk_package="openai",
    chat=ClassSpec(
        class_path="agno.models.openai.chat.OpenAIChat", params=_OPENAI_CHAT_PARAMS, base_url_kwarg="base_url"
    ),
    embedder=ClassSpec(
        class_path="agno.knowledge.embedder.openai.OpenAIEmbedder", params=_EMBEDDER_PARAMS, base_url_kwarg="base_url"
    ),
    default_hosts=frozenset({"api.openai.com"}),
    api_key_env="OPENAI_API_KEY",
)

ANTHROPIC = ProviderSpec(
    id="anthropic",
    sdk_package="anthropic",
    chat=ClassSpec(
        class_path="agno.models.anthropic.claude.Claude",
        params=_same("max_tokens", "temperature", "top_p", "top_k", "timeout"),
    ),
    api_key_env="ANTHROPIC_API_KEY",
)

GEMINI = ProviderSpec(
    id="gemini",
    sdk_package="google-genai",
    aliases=("google",),
    chat=ClassSpec(
        class_path="agno.models.google.gemini.Gemini",
        params=_same(
            "temperature", "top_p", "top_k", "max_output_tokens", "seed", "presence_penalty", "frequency_penalty"
        ),
    ),
    embedder=ClassSpec(class_path="agno.knowledge.embedder.google.GeminiEmbedder", params=_EMBEDDER_PARAMS),
    api_key_env="GEMINI_API_KEY",
)

GROQ = ProviderSpec(
    id="groq",
    sdk_package="groq",
    chat=ClassSpec(
        class_path="agno.models.groq.groq.Groq",
        params=_same(
            "temperature",
            "top_p",
            "max_tokens",
            "frequency_penalty",
            "presence_penalty",
            "seed",
            "stop",
            "timeout",
            "max_retries",
        ),
        base_url_kwarg="base_url",
    ),
    default_hosts=frozenset({"api.groq.com"}),
    api_key_env="GROQ_API_KEY",
)

AZURE = ProviderSpec(
    id="azure",
    sdk_package="openai",
    aliases=("azureopenai",),
    # Sem base_url da config até o F4-01: a chave vai no header ``api-key``, que o httpx não
    # remove em redirect para outra origem (só ``Authorization``). Endpoint só do operador.
    chat=ClassSpec(
        class_path="agno.models.azure.openai_chat.AzureOpenAI",
        params=_OPENAI_CHAT_PARAMS,
        operator_env=(
            OperatorEnv("azure_endpoint", "AZURE_ENDPOINT", required=True),
            OperatorEnv("api_version", "AZURE_VERSION"),
        ),
    ),
    # Como antes: o embedder recebe só a chave (endpoint pelas AZURE_EMBEDDER_* do agno).
    embedder=ClassSpec(
        class_path="agno.knowledge.embedder.azure_openai.AzureOpenAIEmbedder", params=_EMBEDDER_PARAMS
    ),
    api_key_env="AZURE_API_KEY",
)

OPENAI_COMPATIBLE = ProviderSpec(
    id="openai_compatible",
    sdk_package="openai",
    chat=ClassSpec(
        class_path="agno.models.openai.like.OpenAILike", params=_OPENAI_CHAT_PARAMS, base_url_kwarg="base_url"
    ),
    embedder=ClassSpec(
        class_path="agno.knowledge.embedder.openai_like.OpenAILikeEmbedder",
        params=_EMBEDDER_PARAMS,
        base_url_kwarg="base_url",
    ),
    requires_base_url=True,
)

BUILTIN_PROVIDERS: tuple[ProviderSpec, ...] = (OLLAMA, OPENAI, ANTHROPIC, GEMINI, GROQ, AZURE, OPENAI_COMPATIBLE)
