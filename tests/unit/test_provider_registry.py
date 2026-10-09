"""``ProviderRegistry`` (F2-02) com specs fake: adicionar provider = só registrar uma spec.

As specs apontam para classes de ``tests/fakes/providers.py`` (sem SDK nem rede); nada aqui
toca ``domain``/``application``. DNS sempre por resolver injetado.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from src.domain.entities.model_config import ModelConfig
from src.domain.ports.model_factory_port import InvalidModelConfigError
from src.infrastructure.providers import (
    ClassSpec,
    DestinationPolicy,
    OperatorEnv,
    ProviderRegistry,
    ProviderSpec,
)
from tests.fakes.providers import CHAT_PATH, EMBEDDER_PATH, FAILING_PATH, RecordingChatModel, RecordingEmbedder

SECRET = "valor-secreto-F202-nao-pode-vazar"  # noqa: S105 - marcador de vazamento, não é segredo


def _resolver(*addresses: str):
    def resolve(host: str) -> list[str]:
        return list(addresses)

    return resolve


PUBLIC_DNS = _resolver("10.20.30.40")


def _spec(**overrides: object) -> ProviderSpec:
    fields: dict[str, object] = {
        "id": "acme",
        "sdk_package": "acme-sdk",
        "chat": ClassSpec(
            class_path=CHAT_PATH,
            params={"temperature": "temperature", "top_k": "options.top_k", "num_ctx": "options.num_ctx"},
            base_url_kwarg="base_url",
            untrusted_url_kwargs={"client_params.follow_redirects": False},
        ),
        "embedder": ClassSpec(class_path=EMBEDDER_PATH, params={"dimensions": "dimensions"}, base_url_kwarg="host"),
        "aliases": ("acme-ai",),
        "default_hosts": frozenset({"api.acme.example"}),
        "api_key_env": "ACME_API_KEY",
    }
    fields.update(overrides)
    return ProviderSpec(**fields)  # type: ignore[arg-type]


def _registry(*specs: ProviderSpec, **kwargs: object) -> ProviderRegistry:
    kwargs.setdefault("policy", DestinationPolicy(resolver=PUBLIC_DNS))
    return ProviderRegistry(specs or (_spec(),), **kwargs)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def acme_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACME_API_KEY", "chave-do-ambiente")


# ── registro ─────────────────────────────────────────────────────────────────────────────────────


def test_spec_registrada_cria_chat_e_embedder_sem_tocar_domain_nem_application():
    registry = _registry()

    model = registry.create_model(ModelConfig("acme", "acme-chat-1"))
    embedder = registry.create_embedder(ModelConfig("acme", "acme-emb-1"))

    assert isinstance(model, RecordingChatModel)
    assert model.kwargs == {"id": "acme-chat-1", "api_key": "chave-do-ambiente"}
    assert isinstance(embedder, RecordingEmbedder)
    assert embedder.get_embedding("abc") == [3.0, 0.0]


def test_register_depois_do_construtor_tambem_vale():
    registry = ProviderRegistry()
    registry.register(_spec())

    assert registry.supported("chat") == ["acme", "acme-ai"]


@pytest.mark.parametrize("provider", ["acme", "ACME", " acme ", "acme-ai", "Acme-AI"])
def test_id_e_alias_sem_caixa_nem_espacos(provider: str):
    assert _registry().create_model(ModelConfig(provider, "m")).kwargs["id"] == "m"


@pytest.mark.parametrize(
    "duplicate",
    [
        _spec(),
        _spec(id="ACME", aliases=()),
        _spec(id="outro", aliases=("acme",)),
        _spec(id="outro", aliases=("ACME-AI",)),
        _spec(id="acme-ai", aliases=()),
    ],
)
def test_id_ou_alias_duplicado_sem_caixa_e_erro(duplicate: ProviderSpec):
    registry = _registry()

    with pytest.raises(ValueError, match="duplicad"):
        registry.register(duplicate)


def test_provider_desconhecido_lista_os_suportados_do_tipo():
    registry = _registry(_spec(), _spec(id="so-chat", aliases=(), embedder=None))

    with pytest.raises(InvalidModelConfigError, match=r"não suportado.*Suportados: acme, acme-ai, so-chat"):
        registry.create_model(ModelConfig("xyz", "m"))
    with pytest.raises(InvalidModelConfigError, match=r"Suportados: acme, acme-ai$"):
        registry.create_embedder(ModelConfig("so-chat", "m"))
    assert registry.supported("embedder") == ["acme", "acme-ai"]


# ── model_params ─────────────────────────────────────────────────────────────────────────────────


def test_model_params_permitidos_viram_kwargs_inclusive_aninhados():
    model = _registry().create_model(
        ModelConfig("acme", "m", params={"temperature": 0.2, "top_k": 40, "num_ctx": 8192})
    )

    assert model.kwargs["temperature"] == 0.2
    assert model.kwargs["options"] == {"top_k": 40, "num_ctx": 8192}


def test_model_param_fora_da_allowlist_lista_as_permitidas_sem_ecoar_a_chave():
    with pytest.raises(InvalidModelConfigError) as caught:
        _registry().create_model(ModelConfig("acme", "m", params={"headers_SEGREDO": "x"}))

    message = str(caught.value)
    assert "num_ctx, temperature, top_k" in message
    assert "headers_SEGREDO" not in message


def test_embedder_tem_allowlist_propria():
    registry = _registry()

    assert registry.create_embedder(ModelConfig("acme", "e", params={"dimensions": 8})).kwargs["dimensions"] == 8
    with pytest.raises(InvalidModelConfigError, match="permitidas: dimensions"):
        registry.create_embedder(ModelConfig("acme", "e", params={"temperature": 0.1}))


@pytest.mark.parametrize("value", [math.nan, math.inf, -math.inf])
def test_model_param_nao_finito_e_recusado(value: float):
    with pytest.raises(InvalidModelConfigError, match="finit"):
        _registry().create_model(ModelConfig("acme", "m", params={"temperature": value}))


def test_provider_sem_params_permitidos_diz_nenhuma():
    spec = _spec(chat=ClassSpec(class_path=CHAT_PATH))

    with pytest.raises(InvalidModelConfigError, match="permitidas: nenhuma"):
        _registry(spec).create_model(ModelConfig("acme", "m", params={"temperature": 0.1}))


# ── destino: base_url ────────────────────────────────────────────────────────────────────────────


def test_base_url_do_operador_vale_so_para_o_proprio_provider():
    other = _spec(id="outro", aliases=(), default_hosts=frozenset())
    registry = _registry(_spec(), other, operator_base_urls={"acme": "http://acme-interno:8080"})

    assert registry.create_model(ModelConfig("acme", "m")).kwargs["base_url"] == "http://acme-interno:8080"
    assert registry.create_embedder(ModelConfig("acme", "e")).kwargs["host"] == "http://acme-interno:8080"
    assert "base_url" not in registry.create_model(ModelConfig("outro", "m")).kwargs


def test_base_url_do_operador_nao_passa_pela_allowlist_nem_desliga_redirect():
    registry = _registry(operator_base_urls={"acme": "http://169.254.169.254"}, policy=DestinationPolicy())

    kwargs = registry.create_model(ModelConfig("acme", "m")).kwargs

    assert kwargs["base_url"] == "http://169.254.169.254"  # operador é confiável
    assert "client_params" not in kwargs


def test_base_url_da_config_prevalece_sobre_a_do_operador_e_desliga_redirect():
    registry = _registry(operator_base_urls={"acme": "http://acme-interno:8080"})

    kwargs = registry.create_model(ModelConfig("acme", "m", base_url="https://api.acme.example/v2")).kwargs

    assert kwargs["base_url"] == "https://api.acme.example/v2"
    assert kwargs["client_params"] == {"follow_redirects": False}


@pytest.mark.parametrize("url", ["https://api.acme.example", "https://API.ACME.EXAMPLE./v1"])
def test_host_padrao_do_provider_e_aceito(url: str):
    assert _registry().create_model(ModelConfig("acme", "m", base_url=url)).kwargs["base_url"] == url


@pytest.mark.parametrize(
    ("url", "allowlist"),
    [
        ("https://gw.interno.example:8443/v1", ("gw.interno.example",)),
        ("http://10.0.0.5:8000", ("10.0.0.5",)),
        ("http://[fd00::7]:8000", ("fd00::7",)),
        ("http://[fd00:0::7]:8000", ("fd00::7",)),
    ],
)
def test_host_na_allowlist_do_operador_e_aceito(url: str, allowlist: tuple[str, ...]):
    registry = _registry(policy=DestinationPolicy(allowlist=allowlist, resolver=PUBLIC_DNS))

    assert registry.create_model(ModelConfig("acme", "m", base_url=url)).kwargs["base_url"] == url


@pytest.mark.parametrize(
    "url", ["https://evil.example", "https://api.acme.example.evil.example", "http://10.0.0.6", "http://localhost:11434"]
)
def test_host_fora_do_provider_e_da_allowlist_e_recusado(url: str):
    registry = _registry(policy=DestinationPolicy(allowlist=("gw.interno.example", "10.0.0.5"), resolver=PUBLIC_DNS))

    with pytest.raises(InvalidModelConfigError, match="MODEL_BASE_URL_ALLOWLIST") as caught:
        registry.create_model(ModelConfig("acme", "m", base_url=url))
    assert caught.value.__cause__ is None


@pytest.mark.parametrize("url", ["http://localhost:11434", "http://127.0.0.1:8000", "http://[::1]:8000"])
def test_loopback_so_no_modo_dev_local(url: str):
    dev = _registry(policy=DestinationPolicy(allow_loopback=True, resolver=_resolver("127.0.0.1")))
    prod = _registry(policy=DestinationPolicy(resolver=_resolver("127.0.0.1")))

    assert dev.create_model(ModelConfig("acme", "m", base_url=url)).kwargs["base_url"] == url
    with pytest.raises(InvalidModelConfigError):
        prod.create_model(ModelConfig("acme", "m", base_url=url))


@pytest.mark.parametrize(
    "url",
    [
        "http://169.254.169.254/latest",
        "http://2852039166",
        "http://0xa9.0xfe.0xa9.0xfe",
        "http://169.254.43518",
        "http://0251.0376.0251.0376",
        "http://[::ffff:169.254.169.254]",
        "http://[64:ff9b::a9fe:a9fe]",
        "http://[fe80::1]",
        "http://[fd00:ec2::254]",
        "http://100.100.100.200",
    ],
)
def test_metadata_e_link_local_recusados_mesmo_na_allowlist_e_no_modo_dev(url: str):
    host = url.split("//", 1)[1].split("/", 1)[0].strip("[]").lower()
    registry = _registry(policy=DestinationPolicy(allowlist=(host,), allow_loopback=True, resolver=PUBLIC_DNS))

    with pytest.raises(InvalidModelConfigError, match="metadata"):
        registry.create_model(ModelConfig("acme", "m", base_url=url))


def test_host_permitido_que_resolve_para_metadata_e_recusado():
    registry = _registry(
        policy=DestinationPolicy(allowlist=("gw.interno.example",), resolver=_resolver("10.0.0.1", "169.254.169.254"))
    )

    with pytest.raises(InvalidModelConfigError, match="metadata"):
        registry.create_model(ModelConfig("acme", "m", base_url="https://gw.interno.example"))


def test_host_permitido_que_nao_resolve_e_recusado():
    registry = _registry(policy=DestinationPolicy(allowlist=("gw.interno.example",), resolver=_resolver()))

    with pytest.raises(InvalidModelConfigError, match="não resolve"):
        registry.create_model(ModelConfig("acme", "m", base_url="https://gw.interno.example"))


def test_provider_sem_base_url_configuravel_recusa_base_url():
    spec = _spec(chat=ClassSpec(class_path=CHAT_PATH))

    with pytest.raises(InvalidModelConfigError, match="não aceita base_url"):
        _registry(spec).create_model(ModelConfig("acme", "m", base_url="https://api.acme.example"))


def test_provider_que_exige_base_url_recusa_sem_ela():
    spec = _spec(requires_base_url=True)

    with pytest.raises(InvalidModelConfigError, match="exige base_url"):
        _registry(spec).create_model(ModelConfig("acme", "m"))


# ── chave ────────────────────────────────────────────────────────────────────────────────────────


def test_api_key_ref_env_tem_precedencia_sobre_a_chave_do_ambiente(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ACME_TENANT_API_KEY", SECRET)

    model = _registry().create_model(ModelConfig("acme", "m", api_key_ref="env:ACME_TENANT_API_KEY"))

    assert model.kwargs["api_key"] == SECRET


def test_api_key_ref_file_le_de_secrets_dir(tmp_path: Path):
    (tmp_path / "acme").write_text(SECRET + "\n", encoding="utf-8")

    model = _registry(secrets_dir=str(tmp_path)).create_model(
        ModelConfig("acme", "m", api_key_ref=f"file:{tmp_path}/acme")
    )

    assert model.kwargs["api_key"] == SECRET


def test_api_key_ref_que_nao_resolve_e_erro_sem_valor(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ACME_TENANT_API_KEY", raising=False)

    with pytest.raises(InvalidModelConfigError, match="ACME_TENANT_API_KEY") as caught:
        _registry().create_model(ModelConfig("acme", "m", api_key_ref="env:ACME_TENANT_API_KEY"))
    assert caught.value.__cause__ is None


@pytest.mark.parametrize(
    ("config", "reason"),
    [
        (ModelConfig("acme", "m", base_url="https://evil.example"), "MODEL_BASE_URL_ALLOWLIST"),
        (ModelConfig("acme", "m", params={"nao_permitido": 1}), "permitidas"),
        (ModelConfig("acme-sem-sdk", "m"), "pip install"),
    ],
)
def test_segredo_so_e_lido_depois_de_destino_params_e_classe(config: ModelConfig, reason: str):
    """``file:`` inexistente prova que o segredo nem foi lido: o erro é o da etapa anterior."""
    missing_sdk = _spec(id="acme-sem-sdk", aliases=(), chat=ClassSpec(class_path="pacote_inexistente_f202.Modelo"))
    with_ref = ModelConfig(
        config.provider, config.model_id, params=config.params, base_url=config.base_url,
        api_key_ref="file:/run/secrets/nao-existe-f202",
    )

    with pytest.raises(InvalidModelConfigError, match=reason):
        _registry(_spec(), missing_sdk).create_model(with_ref)


def test_chave_do_ambiente_obrigatoria_ausente_cita_so_o_nome(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ACME_API_KEY")

    with pytest.raises(InvalidModelConfigError, match="ACME_API_KEY não configurado"):
        _registry().create_model(ModelConfig("acme", "m"))


def test_provider_sem_chave_do_ambiente_nao_recebe_api_key(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OPENAI_API_KEY", SECRET)

    model = _registry(_spec(api_key_env=None)).create_model(ModelConfig("acme", "m"))

    assert "api_key" not in model.kwargs


def test_classe_sem_kwarg_de_chave_recusa_api_key_ref():
    spec = _spec(embedder=ClassSpec(class_path=EMBEDDER_PATH, api_key_kwarg=None))

    with pytest.raises(InvalidModelConfigError, match="não aceita api_key_ref"):
        _registry(spec).create_embedder(ModelConfig("acme", "e", api_key_ref="env:ACME_API_KEY"))


def test_classe_sem_kwarg_de_chave_nao_le_chave_do_ambiente():
    spec = _spec(embedder=ClassSpec(class_path=EMBEDDER_PATH, api_key_kwarg=None))

    assert _registry(spec).create_embedder(ModelConfig("acme", "e")).kwargs == {"id": "e"}


# ── variáveis do operador ────────────────────────────────────────────────────────────────────────

_AZURE_LIKE = ClassSpec(
    class_path=CHAT_PATH,
    base_url_kwarg="endpoint",
    operator_env=(OperatorEnv("endpoint", "ACME_ENDPOINT", required=True), OperatorEnv("version", "ACME_VERSION")),
)


def test_variaveis_do_operador_viram_kwargs(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ACME_ENDPOINT", "https://acme.example")
    monkeypatch.setenv("ACME_VERSION", "2024-10-21")

    kwargs = _registry(_spec(chat=_AZURE_LIKE)).create_model(ModelConfig("acme", "m")).kwargs

    assert kwargs["endpoint"] == "https://acme.example" and kwargs["version"] == "2024-10-21"


def test_variavel_obrigatoria_do_operador_ausente_e_erro(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ACME_ENDPOINT", raising=False)

    with pytest.raises(InvalidModelConfigError, match="ACME_ENDPOINT não configurado"):
        _registry(_spec(chat=_AZURE_LIKE)).create_model(ModelConfig("acme", "m"))


def test_base_url_da_config_supre_a_variavel_obrigatoria(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("ACME_ENDPOINT", raising=False)
    monkeypatch.delenv("ACME_VERSION", raising=False)

    kwargs = _registry(_spec(chat=_AZURE_LIKE)).create_model(
        ModelConfig("acme", "m", base_url="https://api.acme.example")
    ).kwargs

    assert kwargs["endpoint"] == "https://api.acme.example" and "version" not in kwargs


# ── import e construtor ──────────────────────────────────────────────────────────────────────────


def test_sdk_ausente_diz_o_pacote_numa_linha():
    spec = _spec(chat=ClassSpec(class_path="pacote_inexistente_f202.chat.Modelo"))

    with pytest.raises(InvalidModelConfigError) as caught:
        _registry(spec).create_model(ModelConfig("acme", "m"))

    message = str(caught.value)
    assert "pip install acme-sdk" in message and "\n" not in message
    assert caught.value.__cause__ is None


def test_classe_inexistente_no_modulo_e_erro_claro():
    spec = _spec(chat=ClassSpec(class_path="tests.fakes.providers.NaoExiste"))

    with pytest.raises(InvalidModelConfigError, match=r"tests\.fakes\.providers\.NaoExiste"):
        _registry(spec).create_model(ModelConfig("acme", "m"))


def test_falha_do_construtor_vira_erro_nosso_sem_a_chave_e_sem_encadear(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("ACME_API_KEY", SECRET)
    spec = _spec(chat=ClassSpec(class_path=FAILING_PATH))

    with pytest.raises(InvalidModelConfigError) as caught:
        _registry(spec).create_model(ModelConfig("acme", "m"))

    assert "RuntimeError" in str(caught.value)
    assert SECRET not in str(caught.value)
    assert caught.value.__cause__ is None and caught.value.__context__ is None


def test_dns_recebe_o_host_original_e_a_comparacao_usa_o_normalizado():
    """Nome absoluto (``gw.interno.example.``) e relativo resolvem diferente com search/ndots: o
    resolver confere o mesmo nome que o SDK vai resolver."""
    asked: list[str] = []

    def resolver(host: str) -> list[str]:
        asked.append(host)
        return ["10.0.0.1"]

    registry = _registry(policy=DestinationPolicy(allowlist=("gw.interno.example",), resolver=resolver))

    registry.create_model(ModelConfig("acme", "m", base_url="https://GW.interno.example./v1"))

    assert asked == ["gw.interno.example."]


def test_host_padrao_sem_https_e_recusado_sem_ecoar_o_host():
    with pytest.raises(InvalidModelConfigError, match="https") as caught:
        _registry().create_model(ModelConfig("acme", "m", base_url="http://api.acme.example/v1"))

    assert "api.acme.example" not in str(caught.value)
