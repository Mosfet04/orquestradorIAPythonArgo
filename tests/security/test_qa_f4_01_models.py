"""QA do F4-01: ``base_url`` de modelo/embedder, pelos SDKs reais (``openai``/``ollama``) e rede falsa.

Complementa ``test_f4_01_model_destination.py``: rebinding nos dois caminhos do embedder, redirect no SDK,
formas hostis de ``base_url`` mesmo na allowlist, chave que não vai a destino recusado e o ``groq`` (SDK não
instalado: classe do agno sobre o SDK falso de ``optional_sdks_stubbed``).
"""

from __future__ import annotations

import json
from typing import Any

import openai
import pytest
from agno.exceptions import EmbeddingError

from src.domain.entities.model_config import ModelConfig
from src.domain.entities.validation import InvalidField
from src.domain.ports import InvalidModelConfigError
from src.infrastructure.providers import DestinationPolicy, ProviderRegistry
from src.infrastructure.providers.builtins import BUILTIN_PROVIDERS
from src.infrastructure.security.guarded_http import GuardedHttp
from src.infrastructure.security.ssrf import DestinationRule
from tests.fakes.network_backend import RecordingAsyncBackend, RecordingSyncBackend, http_response

KEY = "sk-QAF401-nao-pode-sair"  # marcador, não é segredo


class _Dns:
    def __init__(self, *answers: list[str]) -> None:
        self.answers = list(answers)
        self.calls: list[str] = []

    def __call__(self, host: str) -> list[str]:
        self.calls.append(host)
        return self.answers.pop(0) if len(self.answers) > 1 else self.answers[0]


def _registry(
    *,
    allowlist: tuple[str, ...] = (),
    dns: _Dns | None = None,
    async_backend: RecordingAsyncBackend | None = None,
    sync_backend: RecordingSyncBackend | None = None,
) -> tuple[ProviderRegistry, GuardedHttp]:
    resolver = dns or _Dns(["10.20.30.40"])
    http = GuardedHttp(
        DestinationRule(public_only=False), resolver=resolver, async_backend=async_backend, sync_backend=sync_backend
    )
    policy = DestinationPolicy(allowlist=allowlist, resolver=resolver)
    return ProviderRegistry(BUILTIN_PROVIDERS, policy=policy, http=http), http


@pytest.fixture(autouse=True)
def provider_keys_in_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OPENAI", "GROQ", "OLLAMA"):
        monkeypatch.setenv(f"{name}_API_KEY", KEY)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)


_EMBEDDING = json.dumps(
    {"object": "list", "data": [{"object": "embedding", "index": 0, "embedding": [0.5]}], "model": "m"}
).encode()


@pytest.mark.parametrize("provider", ["openai", "openai_compatible"])
async def test_embedder_rebinding_para_metadata_e_recusado_no_caminho_sincrono_e_no_assincrono(provider: str):
    ok = http_response(200, _EMBEDDING)
    sync_backend, async_backend = RecordingSyncBackend(ok), RecordingAsyncBackend(ok)
    dns = _Dns(["10.1.1.1"], ["169.254.169.254"])  # 1ª resolução da policy, 2ª e seguintes: metadata
    registry, http = _registry(allowlist=("emb.corp",), dns=dns, async_backend=async_backend, sync_backend=sync_backend)
    try:
        embedder = registry.create_embedder(ModelConfig(provider, "m", base_url="http://emb.corp/v1"))
        embedder.client_params = {"max_retries": 0}  # type: ignore[attr-defined]  # sem espera de backoff do SDK
        with pytest.raises(EmbeddingError):
            embedder.get_embedding("oi")
        with pytest.raises(EmbeddingError):
            await embedder.async_get_embedding("oi")  # type: ignore[attr-defined]
    finally:
        await http.aclose()
    assert sync_backend.connects == [] and async_backend.connects == []
    assert sync_backend.sent() == b"" and async_backend.sent() == b""  # a chave nem chegou ao fio
    # foi a guarda quem resolveu: policy na montagem + uma resolução por conexão (síncrona e assíncrona)
    assert dns.calls == ["emb.corp", "emb.corp", "emb.corp"]


async def test_chat_openai_nao_segue_redirect_e_a_chave_nao_vai_ao_destino_do_redirect():
    redirect = http_response(307, b"", {"Location": "http://169.254.169.254/v1/chat/completions"})
    backend = RecordingAsyncBackend(redirect)
    registry, http = _registry(allowlist=("gw.corp",), dns=_Dns(["10.20.30.40"]), async_backend=backend)
    try:
        model = registry.create_model(ModelConfig("openai_compatible", "m", base_url="http://gw.corp/v1"))
        client = model.get_async_client()  # type: ignore[attr-defined]
        with pytest.raises(openai.APIStatusError):
            await client.with_options(max_retries=0).models.list()
    finally:
        await http.aclose()
    assert backend.connects == [("10.20.30.40", 80)]
    assert b"169.254.169.254" not in backend.sent()


async def test_chat_openai_com_destino_recusado_na_conexao_nao_envia_a_chave():
    backend = RecordingAsyncBackend()
    dns = _Dns(["10.20.30.40"], ["169.254.169.254"])
    registry, http = _registry(allowlist=("gw.corp",), dns=dns, async_backend=backend)
    try:
        model = registry.create_model(
            ModelConfig("openai_compatible", "m", base_url="http://gw.corp/v1", api_key_ref="env:OPENAI_API_KEY")
        )
        client = model.get_async_client()  # type: ignore[attr-defined]
        with pytest.raises(openai.APIConnectionError) as caught:
            await client.with_options(max_retries=0).models.list()
    finally:
        await http.aclose()
    assert backend.connects == [] and backend.sent() == b""
    assert dns.calls == ["gw.corp", "gw.corp"]  # policy na montagem e a conexão, que conferiu e recusou
    assert KEY not in str(caught.value) and "169.254" not in str(caught.value)


@pytest.mark.parametrize(
    ("provider", "url"),
    [
        ("openai_compatible", "http://169.254.169.254/v1"),
        ("openai_compatible", "http://[::ffff:169.254.169.254]/v1"),
        ("openai_compatible", "http://[::ffff:a9fe:a9fe]/v1"),
        ("openai_compatible", "http://2852039166/v1"),
        ("openai_compatible", "http://0xa9.0xfe.0xa9.0xfe/v1"),
        ("openai_compatible", "http://[fd00:ec2::254]/v1"),
        ("openai_compatible", "http://[64:ff9b::a9fe:a9fe]/v1"),
        ("ollama", "http://169.254.169.254:11434"),
        ("openai", "https://169.254.169.254/v1"),
    ],
)
def test_base_url_de_metadata_e_recusada_mesmo_com_o_host_na_allowlist(provider: str, url: str):
    registry, _ = _registry(
        allowlist=("169.254.169.254", "2852039166", "0xa9.0xfe.0xa9.0xfe", "allowed.corp", "::ffff:169.254.169.254"),
        dns=_Dns(["10.20.30.40"]),
    )
    with pytest.raises(InvalidModelConfigError) as caught:
        registry.create_model(ModelConfig(provider, "m", base_url=url))
    assert KEY not in str(caught.value)
    assert "169.254" not in str(caught.value)  # a mensagem não cita o host


def test_base_url_com_credencial_no_userinfo_e_recusada_pela_entidade():
    with pytest.raises(InvalidField, match="credenciais"):
        ModelConfig("openai_compatible", "m", base_url="http://allowed.corp@169.254.169.254/v1")


def test_base_url_que_resolve_para_metadata_na_montagem_e_recusada():
    registry, _ = _registry(allowlist=("gw.corp",), dns=_Dns(["169.254.169.254"]))
    with pytest.raises(InvalidModelConfigError, match="resolve para metadata/link-local; recusado"):
        registry.create_model(ModelConfig("openai_compatible", "m", base_url="http://gw.corp/v1"))


@pytest.mark.usefixtures("optional_sdks_stubbed")
def test_groq_com_base_url_da_config_recebe_o_cliente_async_guardado():
    """O SDK ``groq`` não está instalado: a classe do agno roda sobre o SDK falso; vale o kwarg entregue."""
    registry, http = _registry(allowlist=("gw.corp",))
    model = registry.create_model(ModelConfig("groq", "m", base_url="http://gw.corp/openai/v1"))

    params: dict[str, Any] = model.client_params  # type: ignore[attr-defined]
    assert params["http_client"] is http.async_client()
    assert model.base_url == "http://gw.corp/openai/v1"  # type: ignore[attr-defined]


@pytest.mark.usefixtures("optional_sdks_stubbed")
def test_groq_sem_base_url_da_config_nao_recebe_cliente():
    registry, _ = _registry()
    model = registry.create_model(ModelConfig("groq", "m"))
    assert not model.client_params  # type: ignore[attr-defined]


@pytest.mark.usefixtures("optional_sdks_stubbed")
@pytest.mark.parametrize("provider", ["anthropic", "gemini"])
def test_provider_sem_base_url_na_classe_segue_recusando(provider: str):
    registry, _ = _registry(allowlist=("gw.corp",))
    with pytest.raises(InvalidModelConfigError, match="não aceita base_url"):
        registry.create_model(ModelConfig(provider, "m", base_url="https://gw.corp/v1"))
