"""F2-02: a chave do modelo só vai ao destino do provider ou da ``MODEL_BASE_URL_ALLOWLIST``.

Com os built-ins reais (classes do agno 2.5.8) e DNS falso:
- ``base_url`` fora do provider/allowlist é recusada com ou sem ``api_key_ref`` (os SDKs leem
  chave do ambiente sozinhos), sem ler o segredo (``file:`` inexistente prova) e sem ecoá-lo;
- ``openai_compatible`` sem ``api_key_ref`` nunca recebe a ``OPENAI_API_KEY`` do ambiente;
- metadata/link-local é recusado sempre (literal, formas legadas, IPv6 embutido, DNS), mesmo na
  allowlist e no modo dev local.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.domain.entities.model_config import ModelConfig
from src.domain.ports import InvalidModelConfigError
from src.infrastructure.providers import DestinationPolicy, ProviderRegistry
from src.infrastructure.providers.builtins import BUILTIN_PROVIDERS

ENV_KEY = "sk-AMBIENTE-F202-nao-pode-sair"
FILE_KEY = "sk-ARQUIVO-F202-nao-pode-sair"
MISSING_FILE_REF = "file:/run/secrets/nao-existe-f202"
# Providers cujo modelo de chat aceita base_url (anthropic, gemini e azure recusam qualquer uma).
WITH_BASE_URL = ("ollama", "openai", "groq", "openai_compatible")


def _public_dns(host: str) -> list[str]:
    return ["10.20.30.40"]


def _registry(
    *, allowlist: tuple[str, ...] = (), dev: bool = False, resolver=_public_dns, secrets_dir: str = "/run/secrets"
) -> ProviderRegistry:
    policy = DestinationPolicy(allowlist=allowlist, allow_loopback=dev, resolver=resolver)
    return ProviderRegistry(BUILTIN_PROVIDERS, secrets_dir=secrets_dir, policy=policy)


@pytest.fixture(autouse=True)
def provider_keys_in_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Toda chave de provider no ambiente: se alguma escapasse para o host errado, o teste veria."""
    for name in ("OPENAI", "GROQ", "AZURE", "OLLAMA", "GEMINI", "ANTHROPIC"):
        monkeypatch.setenv(f"{name}_API_KEY", ENV_KEY)
    monkeypatch.setenv("AZURE_ENDPOINT", "https://tenant.openai.azure.com")
    monkeypatch.delenv("OPENAI_BASE_URL", raising=False)


# ── destino fora do provider/allowlist ──────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("provider", "kind"),
    [(p, "chat") for p in WITH_BASE_URL] + [(p, "embedder") for p in WITH_BASE_URL if p != "groq"],
)
def test_base_url_fora_da_allowlist_com_api_key_ref_recusa_sem_ler_o_segredo(provider: str, kind: str):
    registry = _registry(allowlist=("gw.permitido.example",))
    config = ModelConfig(provider, "m", base_url="https://coletor.evil.example/v1", api_key_ref=MISSING_FILE_REF)
    create = registry.create_model if kind == "chat" else registry.create_embedder

    with pytest.raises(InvalidModelConfigError, match="MODEL_BASE_URL_ALLOWLIST") as caught:
        create(config)

    # Se o segredo tivesse sido lido antes, o erro seria o do arquivo inexistente.
    assert "nao-existe-f202" not in str(caught.value)
    assert ENV_KEY not in str(caught.value) and "evil" not in str(caught.value)


@pytest.mark.parametrize("provider", WITH_BASE_URL)
def test_base_url_fora_da_allowlist_sem_api_key_ref_tambem_recusa(provider: str):
    """O SDK mandaria a chave do ambiente (OPENAI_API_KEY, OLLAMA_API_KEY...) ao host do documento."""
    with pytest.raises(InvalidModelConfigError, match="MODEL_BASE_URL_ALLOWLIST"):
        _registry().create_model(ModelConfig(provider, "m", base_url="https://coletor.evil.example/v1"))


@pytest.mark.parametrize("url", ["http://localhost:8000/v1", "http://127.0.0.1:8000/v1", "http://[::1]:8000/v1"])
def test_loopback_fora_do_modo_dev_local_e_recusado(url: str):
    loopback_dns = _registry(resolver=lambda host: ["127.0.0.1"])

    with pytest.raises(InvalidModelConfigError, match="MODEL_BASE_URL_ALLOWLIST"):
        loopback_dns.create_model(ModelConfig("openai_compatible", "m", base_url=url))


def test_host_permitido_recebe_a_chave_do_api_key_ref(tmp_path: Path):
    (tmp_path / "gw").write_text(FILE_KEY, encoding="utf-8")
    registry = _registry(allowlist=("gw.permitido.example",), secrets_dir=str(tmp_path))

    config = ModelConfig(
        "openai_compatible", "m", base_url="https://gw.permitido.example/v1", api_key_ref=f"file:{tmp_path}/gw"
    )

    model = registry.create_model(config)

    assert (model.base_url, model.api_key) == ("https://gw.permitido.example/v1", FILE_KEY)  # type: ignore[attr-defined]


def test_host_padrao_do_provider_recebe_a_chave_sem_allowlist():
    model = _registry().create_model(ModelConfig("openai", "gpt-4o-mini", base_url="https://api.openai.com/v1"))

    assert model.api_key == ENV_KEY  # type: ignore[attr-defined]


# ── openai_compatible nunca herda a OPENAI_API_KEY ──────────────────────────────────────────────


def test_openai_compatible_sem_api_key_ref_nao_recebe_a_chave_do_ambiente():
    registry = _registry(allowlist=("gw.permitido.example",))
    config = ModelConfig("openai_compatible", "m", base_url="https://gw.permitido.example/v1")

    model = registry.create_model(config)
    embedder = registry.create_embedder(config)

    # Cliente do SDK montado como no primeiro request (sem rede): a chave é o placeholder.
    assert model.get_client().api_key == "not-provided"  # type: ignore[attr-defined]
    assert embedder.client.api_key == "not-provided"  # type: ignore[attr-defined]
    assert str(model.get_client().base_url).startswith("https://gw.permitido.example/v1")  # type: ignore[attr-defined]


# ── metadata/link-local: sempre recusados ───────────────────────────────────────────────────────

METADATA_URLS = [
    "http://169.254.169.254/latest/meta-data",
    "http://2852039166/",
    "http://0xa9.0xfe.0xa9.0xfe/",
    "http://169.254.43518/",
    "http://0251.0376.0251.0376/",
    "http://[::ffff:169.254.169.254]/",
    "http://[::ffff:a9fe:a9fe]/",
    "http://[64:ff9b::a9fe:a9fe]/",
    "http://[2002:a9fe:a9fe::]/",
    "http://[fe80::1]/",
    "http://[fd00:ec2::254]/",
    "http://100.100.100.200/",
    "http://192.0.0.192/",
]


@pytest.mark.parametrize("provider", WITH_BASE_URL)
@pytest.mark.parametrize("url", METADATA_URLS)
def test_metadata_recusado_mesmo_na_allowlist_e_no_modo_dev(provider: str, url: str):
    host = url.split("//", 1)[1].split("/", 1)[0].strip("[]")
    registry = _registry(allowlist=(host,), dev=True)

    with pytest.raises(InvalidModelConfigError, match="metadata"):
        registry.create_model(ModelConfig(provider, "m", base_url=url, api_key_ref=MISSING_FILE_REF))


@pytest.mark.parametrize("answer", [["169.254.169.254"], ["10.0.0.1", "fe80::1%eth0"], ["::ffff:169.254.169.254"]])
def test_host_permitido_que_resolve_para_metadata_e_recusado(answer: list[str]):
    registry = _registry(allowlist=("gw.permitido.example",), dev=True, resolver=lambda host: answer)

    with pytest.raises(InvalidModelConfigError, match="metadata"):
        registry.create_model(ModelConfig("openai_compatible", "m", base_url="https://gw.permitido.example/v1"))


# ── rodada 2 (R1): host do provider só com TLS ──────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("provider", "url"),
    [("openai", "http://api.openai.com/v1"), ("groq", "http://api.groq.com"), ("ollama", "http://ollama.com")],
)
def test_host_padrao_do_provider_sem_https_e_recusado_com_a_chave_no_ambiente(provider: str, url: str):
    """A chave (``OPENAI_API_KEY``, Bearer do ``OLLAMA_API_KEY``...) sairia em texto claro."""
    with pytest.raises(InvalidModelConfigError, match="https") as caught:
        _registry().create_model(ModelConfig(provider, "m", base_url=url))

    assert ENV_KEY not in str(caught.value)
    assert url.split("//", 1)[1].split("/", 1)[0] not in str(caught.value)


def test_host_padrao_do_provider_com_https_continua_aceito():
    openai = _registry().create_model(ModelConfig("openai", "m", base_url="https://api.openai.com/v1"))
    ollama = _registry().create_model(ModelConfig("ollama", "m", base_url="https://ollama.com"))

    assert openai.base_url == "https://api.openai.com/v1"  # type: ignore[attr-defined]
    assert ollama.host == "https://ollama.com"  # type: ignore[attr-defined]


def test_groq_com_https_passa_pela_guarda_e_para_no_sdk_ausente():
    """O SDK ``groq`` não está instalado: chegar ao erro de import prova que o destino foi aceito."""
    with pytest.raises(InvalidModelConfigError, match="pip install groq"):
        _registry().create_model(ModelConfig("groq", "m", base_url="https://api.groq.com"))


def test_allowlist_e_loopback_em_dev_seguem_aceitando_http():
    """vLLM/gateway interno sem TLS é decisão do operador (allowlist) ou do dev local."""
    allowlisted = _registry(allowlist=("vllm.interno",)).create_model(
        ModelConfig("openai_compatible", "m", base_url="http://vllm.interno:8000/v1")
    )
    loopback = _registry(dev=True, resolver=lambda host: ["127.0.0.1"]).create_model(
        ModelConfig("openai_compatible", "m", base_url="http://localhost:8000/v1")
    )

    assert allowlisted.base_url == "http://vllm.interno:8000/v1"  # type: ignore[attr-defined]
    assert loopback.base_url == "http://localhost:8000/v1"  # type: ignore[attr-defined]


def test_host_padrao_exige_https_mesmo_na_allowlist():
    """Endpoint público de provider nunca precisa de http; listá-lo não reabre o texto claro (N6)."""
    registry = _registry(allowlist=("api.openai.com",))

    with pytest.raises(InvalidModelConfigError, match="https"):
        registry.create_model(ModelConfig("openai", "m", base_url="http://api.openai.com/v1"))
    https = registry.create_model(ModelConfig("openai", "m", base_url="https://api.openai.com/v1"))
    assert https.base_url == "https://api.openai.com/v1"  # type: ignore[attr-defined]


# ── rodada 2 (R5): azure não aceita base_url da config até o F4-01 ─────────────────────────────


@pytest.mark.parametrize("kind", ["chat", "embedder"])
def test_azure_recusa_base_url_da_config_mesmo_na_allowlist(kind: str):
    """O header ``api-key`` do Azure não é removido pelo httpx em redirect cross-origin."""
    registry = _registry(allowlist=("tenant.openai.azure.com",))
    create = registry.create_model if kind == "chat" else registry.create_embedder

    with pytest.raises(InvalidModelConfigError, match="não aceita base_url"):
        create(ModelConfig("azure", "m", base_url="https://tenant.openai.azure.com"))


def test_azure_continua_usando_o_endpoint_do_operador():
    model = _registry().create_model(ModelConfig("azure", "gpt-4"))

    assert model.azure_endpoint == "https://tenant.openai.azure.com"  # type: ignore[attr-defined]
