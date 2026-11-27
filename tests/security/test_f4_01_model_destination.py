"""F4-01: ``base_url`` de modelo/embedder vinda da config, guardada também na conexão.

- N9: ``https`` exigido sobre a união dos hosts padrão de todos os providers, não só do escolhido;
- N10: loopback do modo dev local decidido pelo host como veio (``localhost.`` não é loopback literal);
- o SDK recebe o cliente httpx guardado (sem redirect, TCP no IP conferido a cada conexão: fecha o DNS
  rebinding): ``openai``/``openai_compatible``/``groq`` no chat (async; o caminho síncrono, que o app não
  usa, falha fechado), os dois caminhos dos embedders OpenAI e o transporte duplo do ``ollama``;
- sem ``base_url`` da config, nada muda (cliente padrão do SDK, destino do provider ou do operador).

Classes reais do agno 3.1.12 e dos SDKs ``openai``/``ollama``; rede e DNS falsos.
"""

from __future__ import annotations

import json

import httpx
import openai
import pytest

from src.domain.entities.model_config import ModelConfig
from src.domain.ports import InvalidModelConfigError
from src.infrastructure.providers import DestinationPolicy, ProviderRegistry
from src.infrastructure.providers.builtins import BUILTIN_PROVIDERS, GROQ, OPENAI, OPENAI_COMPATIBLE
from src.infrastructure.security.guarded_http import GuardedHttp, GuardedTransport
from src.infrastructure.security.ssrf import DestinationRule
from tests.fakes.network_backend import RecordingAsyncBackend, RecordingSyncBackend, http_response

KEY = "sk-F401-nao-pode-sair"  # marcador, não é segredo


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
    dev: bool = False,
    dns: _Dns | None = None,
    async_backend: RecordingAsyncBackend | None = None,
    sync_backend: RecordingSyncBackend | None = None,
) -> tuple[ProviderRegistry, GuardedHttp]:
    resolver = dns or _Dns(["10.20.30.40"])
    http = GuardedHttp(
        DestinationRule(public_only=False), resolver=resolver, async_backend=async_backend, sync_backend=sync_backend
    )
    policy = DestinationPolicy(allowlist=allowlist, allow_loopback=dev, resolver=resolver)
    return ProviderRegistry(BUILTIN_PROVIDERS, policy=policy, http=http), http


@pytest.fixture(autouse=True)
def provider_keys_in_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("OPENAI", "GROQ", "OLLAMA"):
        monkeypatch.setenv(f"{name}_API_KEY", KEY)
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)


# ── N9 / N10 ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("provider", "url"),
    [
        ("openai_compatible", "http://api.openai.com/v1"),
        ("ollama", "http://api.openai.com"),
        ("openai_compatible", "http://api.groq.com/openai/v1"),
        ("openai", "http://OLLAMA.com./v1"),
    ],
)
def test_n9_host_padrao_de_qualquer_provider_exige_https_mesmo_listado(provider: str, url: str):
    registry, _ = _registry(allowlist=("api.openai.com", "api.groq.com", "ollama.com"))
    with pytest.raises(InvalidModelConfigError, match="https"):
        registry.create_model(ModelConfig(provider, "m", base_url=url))


def test_n9_o_mesmo_host_com_https_continua_aceito():
    registry, _ = _registry(allowlist=("api.openai.com",))
    model = registry.create_model(ModelConfig("openai_compatible", "m", base_url="https://api.openai.com/v1"))
    assert model.base_url == "https://api.openai.com/v1"  # type: ignore[attr-defined]


@pytest.mark.parametrize("url", ["http://localhost.:8000/v1", "http://LOCALHOST.:8000/v1"])
def test_n10_localhost_com_ponto_final_nao_conta_como_loopback_no_modo_dev(url: str):
    registry, _ = _registry(dev=True, dns=_Dns(["127.0.0.1"]))
    with pytest.raises(InvalidModelConfigError, match="MODEL_BASE_URL_ALLOWLIST"):
        registry.create_model(ModelConfig("openai_compatible", "m", base_url=url))


def test_n10_localhost_literal_segue_aceito_no_modo_dev():
    registry, _ = _registry(dev=True, dns=_Dns(["127.0.0.1"]))
    model = registry.create_model(ModelConfig("openai_compatible", "m", base_url="http://localhost:8000/v1"))
    assert model.base_url == "http://localhost:8000/v1"  # type: ignore[attr-defined]


# ── cliente guardado no SDK ─────────────────────────────────────────


def test_specs_do_sdk_openai_entregam_o_cliente_async_guardado_no_chat():
    for spec in (OPENAI, OPENAI_COMPATIBLE, GROQ):
        assert spec.chat is not None
        assert dict(spec.chat.guarded_http) == {"client_params.http_client": "async_client"}


async def test_chat_openai_compativel_conecta_no_ip_conferido_e_recusa_rebinding():
    dns = _Dns(["10.20.30.40"], ["10.20.30.40"], ["169.254.169.254"])
    backend = RecordingAsyncBackend(http_response(200, json.dumps({"object": "list", "data": []}).encode()))
    registry, http = _registry(allowlist=("gw.corp",), dns=dns, async_backend=backend)
    try:
        model = registry.create_model(ModelConfig("openai_compatible", "m", base_url="http://gw.corp:8080/v1"))
        client = model.get_async_client()  # type: ignore[attr-defined]
        await client.models.list()
        with pytest.raises(openai.APIConnectionError):
            await client.with_options(max_retries=0).models.list()
    finally:
        await http.aclose()
    assert backend.connects == [("10.20.30.40", 8080)]
    assert b"Host: gw.corp:8080\r\n" in backend.sent()


def test_chat_sincrono_com_base_url_da_config_falha_fechado():
    """O caminho síncrono (``Agent.run``) não é usado pelo app; sem cliente síncrono guardado, não sai."""
    registry, _ = _registry(allowlist=("gw.corp",))
    model = registry.create_model(ModelConfig("openai_compatible", "m", base_url="http://gw.corp/v1"))
    with pytest.raises(TypeError, match="http_client"):
        model.get_client()  # type: ignore[attr-defined]


def test_cliente_guardado_nao_segue_redirect():
    registry, http = _registry(allowlist=("gw.corp",))
    model = registry.create_model(ModelConfig("openai_compatible", "m", base_url="http://gw.corp/v1"))
    client = model.client_params["http_client"]  # type: ignore[attr-defined]
    assert client is http.async_client()
    assert client.follow_redirects is False


@pytest.mark.parametrize("provider", ["openai", "openai_compatible"])
async def test_embedder_openai_usa_o_guardado_nos_dois_caminhos(provider: str):
    body = json.dumps(
        {"object": "list", "data": [{"object": "embedding", "index": 0, "embedding": [0.5, 0.25]}], "model": "m"}
    ).encode()
    sync_backend, async_backend = RecordingSyncBackend(http_response(200, body)), RecordingAsyncBackend(
        http_response(200, body)
    )
    registry, http = _registry(
        allowlist=("emb.corp",), dns=_Dns(["10.1.1.1"]), async_backend=async_backend, sync_backend=sync_backend
    )
    try:
        embedder = registry.create_embedder(ModelConfig(provider, "m", base_url="http://emb.corp/v1"))
        assert embedder.get_embedding("oi") == [0.5, 0.25]
        assert await embedder.async_get_embedding("oi") == [0.5, 0.25]  # type: ignore[attr-defined]
    finally:
        await http.aclose()
    assert sync_backend.connects == [("10.1.1.1", 80)]
    assert async_backend.connects == [("10.1.1.1", 80)]


def test_ollama_recebe_o_transporte_duplo_e_o_sdk_conecta_no_ip_conferido():
    sync_backend = RecordingSyncBackend(http_response(200, b'{"models": []}'))
    registry, _ = _registry(allowlist=("ollama.corp",), dns=_Dns(["10.2.2.2"]), sync_backend=sync_backend)
    model = registry.create_model(ModelConfig("ollama", "llama3", base_url="http://ollama.corp:11434"))
    assert isinstance(model.client_params["transport"], GuardedTransport)  # type: ignore[attr-defined]
    model.get_client().list()  # type: ignore[attr-defined]
    assert sync_backend.connects == [("10.2.2.2", 11434)]


def test_ollama_embedder_recebe_o_transporte_duplo():
    registry, _ = _registry(allowlist=("ollama.corp",))
    embedder = registry.create_embedder(ModelConfig("ollama", "nomic", base_url="http://ollama.corp:11434"))
    assert isinstance(embedder.client_kwargs["transport"], GuardedTransport)  # type: ignore[attr-defined]
    assert embedder.client_kwargs["follow_redirects"] is False  # type: ignore[attr-defined]


def test_sem_base_url_da_config_o_sdk_fica_com_o_cliente_padrao():
    registry, _ = _registry()
    model = registry.create_model(ModelConfig("openai", "gpt-4o"))
    embedder = registry.create_embedder(ModelConfig("openai", "text-embedding-3-small"))
    assert model.client_params is None  # type: ignore[attr-defined]
    assert embedder.sync_http_client is None and embedder.async_http_client is None  # type: ignore[attr-defined]


async def test_aclose_do_registry_fecha_os_clientes_guardados():
    registry, _ = _registry(allowlist=("gw.corp",))
    model = registry.create_model(ModelConfig("openai_compatible", "m", base_url="http://gw.corp/v1"))
    client: httpx.AsyncClient = model.client_params["http_client"]  # type: ignore[attr-defined]
    await registry.aclose()
    assert client.is_closed


def test_copia_do_modelo_mantem_o_cliente_guardado():
    """Cópia do modelo (``Model.__deepcopy__`` do agno; o ``Agent.deep_copy`` por run compartilha o modelo): o
    cliente/transporte guardado não se copia (o deepcopy dele falha no ``SSLContext`` e o agno cai na cópia rasa)."""
    import copy

    from agno.agent import Agent

    registry, _ = _registry(allowlist=("gw.corp", "ollama.corp"))
    for config in (
        ModelConfig("openai_compatible", "m", base_url="http://gw.corp/v1"),
        ModelConfig("ollama", "llama3", base_url="http://ollama.corp:11434"),
    ):
        model = registry.create_model(config)
        assert Agent(id="a", model=model, telemetry=False).deep_copy().model is model
        copied = copy.deepcopy(model)
        assert copied is not model
        original = dict(model.client_params)  # type: ignore[attr-defined]
        same = {key: value is original[key] for key, value in copied.client_params.items()}  # type: ignore[attr-defined]
        assert same == dict.fromkeys(original, True)
