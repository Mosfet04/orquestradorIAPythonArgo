"""QA F2-02: startup real (AppFactory + repositórios Mongo sobre coleção fake) com o
``ProviderRegistry`` montado pelo composition root (``build_provider_registry``) e as classes reais do agno.

Só a rede é falsa: o DNS (``socket.getaddrinfo``) e a coleção Mongo. Critérios cobertos:
(a) documento legado sobe igual, com a classe real do agno e a chave do ambiente de sempre;
(b) campos novos (``model_params``, ``base_url``, ``api_key_ref``) chegam ao modelo real, e o que o
    registry recusa isola só o documento, com o motivo no log e sem host, chave nem valor de param
    em log ou resposta HTTP;
(c) criação concorrente (gather de agentes) sem estado cruzado e fora do event loop, DNS inclusive;
(d) o mesmo documento com loopback sobe no modo dev local e é recusado fora dele;
(e) ``OLLAMA_BASE_URL`` chega ao cliente real do Ollama.
"""

from __future__ import annotations

import math
import socket
from typing import Any

import pytest
from agno.models.openai.like import OpenAILike

from src.application.services.document_indexing_service import DocumentIndexingService
from src.domain.entities.model_config import ModelConfig
from src.domain.entities.rag_config import RagConfig, SearchStrategy
from src.domain.ports import IEmbedderFactory, IModelFactory, InvalidModelConfigError
from src.domain.ports.embedder_factory_port import TextEmbedder
from src.domain.ports.model_factory_port import ChatModel
from src.domain.ports.summary_generator_port import ISummaryGenerator
from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.dependency_injection import build_provider_registry
from src.infrastructure.parsers.text_document_parser import TextDocumentParser
from src.infrastructure.providers import ClassSpec, DestinationPolicy, ProviderRegistry, ProviderSpec
from tests.fakes import InMemoryDocumentTreeRepository, RecordingLogger, running_on_event_loop
from tests.fakes.app_boot import agent_doc, boot, team_doc
from tests.fakes.providers import EMBEDDER_PATH

pytestmark = pytest.mark.usefixtures("offline_knowledge")

KEY_VALUE = "sk-QA-F202-VALOR-DA-CHAVE-NAO-PODE-VAZAR"
ENV_KEY_VALUE = "sk-QA-F202-CHAVE-DO-AMBIENTE-NAO-PODE-VAZAR"
PARAM_VALUE = "valor-de-param-QA-F202-nao-pode-vazar"
GATEWAY = "gw.permitido.invalid"
GATEWAY_URL = f"https://{GATEWAY}/v1"
EVIL_HOST = "coletor.evil.invalid"

COMPAT = {"factory_ia_model": "openai_compatible"}
_PROVIDER_ENV = (
    "OPENAI_API_KEY", "ANTHROPIC_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "AZURE_API_KEY", "AZURE_ENDPOINT",
    "AZURE_VERSION", "OLLAMA_API_KEY", "OLLAMA_BASE_URL", "OPENAI_BASE_URL", "MODEL_BASE_URL_ALLOWLIST",
    "SECRETS_DIR", "ENVIRONMENT", "APP_HOST", "API_KEY_RUN", "API_KEY_ADMIN",
)


class FakeDns:
    """``socket.getaddrinfo`` falso: registra nome e thread de cada consulta; nunca sai da máquina."""

    def __init__(self, answers: dict[str, str] | None = None) -> None:
        self.answers = answers or {}
        self.queries: list[tuple[str, bool]] = []

    def __call__(self, host: str, *args: Any, **kwargs: Any) -> list[tuple[Any, ...]]:
        self.queries.append((host, running_on_event_loop()))
        address = self.answers.get(host, "10.20.30.40")
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (address, 0))]


@pytest.fixture
def dns(monkeypatch: pytest.MonkeyPatch) -> FakeDns:
    fake = FakeDns()
    monkeypatch.setattr(socket, "getaddrinfo", fake)
    return fake


@pytest.fixture(autouse=True)
def clean_provider_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in _PROVIDER_ENV:
        monkeypatch.delenv(name, raising=False)


def registry_from_env(monkeypatch: pytest.MonkeyPatch, **env: str) -> ProviderRegistry:
    """O registry exatamente como o composition root o monta para este ambiente."""
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    return build_provider_registry(AppConfig.load(), RecordingLogger())


class Spy(IModelFactory, IEmbedderFactory):
    """Delega ao registry real e guarda o que foi criado e em que thread."""

    def __init__(self, inner: ProviderRegistry) -> None:
        self.inner = inner
        self.models: dict[str, ChatModel] = {}
        self.embedders: dict[str, TextEmbedder] = {}
        self.on_event_loop: list[bool] = []

    def create_model(self, config: ModelConfig) -> ChatModel:
        self.on_event_loop.append(running_on_event_loop())
        model = self.inner.create_model(config)
        self.models[config.model_id] = model
        return model

    def create_embedder(self, config: ModelConfig) -> TextEmbedder:
        self.on_event_loop.append(running_on_event_loop())
        embedder = self.inner.create_embedder(config)
        self.embedders[config.model_id] = embedder
        return embedder


def startup(
    monkeypatch: pytest.MonkeyPatch, spy: Spy, agents: list[dict[str, Any]], teams: list[dict[str, Any]] | None = None
) -> Any:
    return boot(monkeypatch, agents, teams or [], spy)  # type: ignore[arg-type]


def failures(result: Any) -> dict[str, dict[str, Any]]:
    return {
        c.get("agent_id") or c.get("team_id"): c for m, c in result.errors() if m.endswith("não carregado")
    }


def agent_ids(result: Any) -> list[str]:
    """Ids em /agents; sem nenhum agente o AgentOS não monta a rota (404 com ``detail``)."""
    return sorted(a["id"] for a in result.agents) if isinstance(result.agents, list) else []


def team_ids(result: Any) -> list[str]:
    return sorted(t["id"] for t in result.teams) if isinstance(result.teams, list) else []


def assert_nothing_leaked(result: Any, *forbidden: str) -> None:
    blob = result.everything()
    for item in forbidden:
        assert item not in blob, item


# ── (a) legado com classe real e chave do ambiente ──────────────────────────────────────────────


def test_legado_sobe_com_classes_reais_chave_do_ambiente_e_isola_so_o_que_falta(
    monkeypatch: pytest.MonkeyPatch, dns: FakeDns
):
    monkeypatch.setenv("OPENAI_API_KEY", ENV_KEY_VALUE)
    monkeypatch.setenv("AZURE_ENDPOINT", "https://tenant.openai.azure.com")
    spy = Spy(registry_from_env(monkeypatch))
    docs = [
        agent_doc("com-chave", factory_ia_model="openai", model="gpt-4o-mini"),
        agent_doc("ollama"),  # sem provider: ollama, como sempre
        agent_doc("azure-sem-chave", factory_ia_model="azure", model="gpt-4"),
        agent_doc("desconhecido", factory_ia_model="foo", model="x"),
        agent_doc("camel", factoryIaModel="google", model="gemini-2.0"),
    ]

    result = startup(monkeypatch, spy, docs)

    assert result.status == (200, 200, 200)
    assert agent_ids(result) == ["com-chave", "ollama"]
    assert type(spy.models["gpt-4o-mini"]).__qualname__ == "OpenAIChat"
    assert spy.models["gpt-4o-mini"].api_key == ENV_KEY_VALUE  # type: ignore[attr-defined]
    assert type(spy.models["llama3.2:latest"]).__qualname__ == "Ollama"
    reasons = {agent_id: ctx["reason"] for agent_id, ctx in failures(result).items()}
    assert reasons["azure-sem-chave"] == "AZURE_API_KEY não configurado"
    supported = ", ".join(spy.inner.supported("chat"))
    assert "openai_compatible" in supported
    assert reasons["desconhecido"] == f"Tipo 'foo' não suportado para modelo. Suportados: {supported}"
    assert reasons["camel"] == "GEMINI_API_KEY não configurado"
    assert dns.queries == []  # sem base_url da config, ninguém consulta DNS
    assert_nothing_leaked(result, ENV_KEY_VALUE)


# ── (b) campos novos no modelo real; recusas isolam o documento ─────────────────────────────────


def test_campos_novos_chegam_ao_modelo_real_e_a_chave_so_vai_ao_host_da_allowlist(
    monkeypatch: pytest.MonkeyPatch, dns: FakeDns
):
    monkeypatch.setenv("QA_F202_GW_API_KEY", KEY_VALUE)
    monkeypatch.setenv("OPENAI_API_KEY", ENV_KEY_VALUE)
    spy = Spy(registry_from_env(monkeypatch, MODEL_BASE_URL_ALLOWLIST=f" {GATEWAY.upper()} , outro.invalid"))
    doc = agent_doc(
        "gw", model="modelo-gw", base_url=GATEWAY_URL, api_key_ref="env:QA_F202_GW_API_KEY",
        model_params={"temperature": 0.25, "max_tokens": 77}, **COMPAT,
    )
    sem_ref = agent_doc("gw-sem-ref", model="modelo-sem-ref", base_url=GATEWAY_URL, **COMPAT)

    result = startup(monkeypatch, spy, [doc, sem_ref])

    assert agent_ids(result) == ["gw", "gw-sem-ref"]
    model = spy.models["modelo-gw"]
    assert isinstance(model, OpenAILike)
    assert (model.base_url, model.api_key, model.temperature, model.max_tokens) == (GATEWAY_URL, KEY_VALUE, 0.25, 77)
    # sem api_key_ref: placeholder do SDK; a OPENAI_API_KEY do processo não vai para o gateway
    client = spy.models["modelo-sem-ref"].get_client()  # type: ignore[attr-defined]
    assert client.api_key == "not-provided" and str(client.base_url).startswith(GATEWAY_URL)
    assert [host for host, _ in dns.queries] == [GATEWAY, GATEWAY]
    assert_nothing_leaked(result, KEY_VALUE, ENV_KEY_VALUE, GATEWAY, "QA_F202_GW_API_KEY")


@pytest.mark.parametrize(
    ("fields", "reason"),
    [
        ({"base_url": f"https://{EVIL_HOST}/v1", "api_key_ref": "env:QA_F202_GW_API_KEY"}, "MODEL_BASE_URL_ALLOWLIST"),
        ({"base_url": f"https://{EVIL_HOST}/v1"}, "MODEL_BASE_URL_ALLOWLIST"),  # sem ref: a chave do ambiente iria
        ({"base_url": "http://169.254.169.254/latest"}, "metadata"),
        ({"base_url": "http://2852039166/"}, "metadata"),
        ({"base_url": "http://[::ffff:a9fe:a9fe]/"}, "metadata"),
        ({"base_url": GATEWAY_URL, "model_params": {"headers": PARAM_VALUE}}, "model_params"),
        ({"base_url": GATEWAY_URL, "model_params": {"temperature": math.nan}}, "finito"),
        ({"base_url": GATEWAY_URL, "model_params": {"max_tokens": math.inf}}, "finito"),
        ({"base_url": GATEWAY_URL, "api_key_ref": "env:QA_F202_AUSENTE_API_KEY"}, "QA_F202_AUSENTE_API_KEY"),
        ({"base_url": GATEWAY_URL, "api_key_ref": "file:/run/secrets/nao-existe-f202"}, "nao-existe-f202"),
    ],
    ids=lambda v: v if isinstance(v, str) else next(iter(v)),
)
def test_documento_recusado_pelo_registry_real_e_isolado_com_motivo_e_sem_vazamento(
    monkeypatch: pytest.MonkeyPatch, dns: FakeDns, fields: dict[str, Any], reason: str
):
    monkeypatch.setenv("QA_F202_GW_API_KEY", KEY_VALUE)
    monkeypatch.setenv("OPENAI_API_KEY", ENV_KEY_VALUE)
    spy = Spy(registry_from_env(monkeypatch, MODEL_BASE_URL_ALLOWLIST=GATEWAY))
    docs = [
        agent_doc("antes"),
        agent_doc("ruim", model="modelo-ruim", **COMPAT, **fields),
        agent_doc("depois", model="modelo-depois", base_url=GATEWAY_URL, **COMPAT),
    ]

    result = startup(monkeypatch, spy, docs, [team_doc("t-ruim", ["antes"], base_url=fields["base_url"])])

    assert result.status == (200, 200, 200)
    assert agent_ids(result) == ["antes", "depois"]
    bad = failures(result)["ruim"]
    assert bad["error_type"] == "InvalidModelConfigError" and reason in bad["reason"]
    assert_nothing_leaked(
        result, KEY_VALUE, ENV_KEY_VALUE, PARAM_VALUE, EVIL_HOST, "169.254.169.254", "2852039166",
        "a9fe:a9fe", "QA_F202_GW_API_KEY",
    )
    # o team (provider ollama) com a mesma base_url: recusado se ela é de fora, aceito se é o gateway
    team_refused = GATEWAY not in fields["base_url"]
    assert ("t-ruim" in failures(result)) is team_refused
    assert (team_ids(result) == []) is team_refused


@pytest.mark.parametrize("fields", [{"base_url": "https://api.anthropic.com"}, {"base_url": GATEWAY_URL}])
@pytest.mark.parametrize("provider", ["gemini", "azure", "anthropic"])
def test_provider_sem_base_url_configuravel_isola_o_documento(
    monkeypatch: pytest.MonkeyPatch, dns: FakeDns, provider: str, fields: dict[str, Any]
):
    for name in ("GEMINI", "AZURE", "ANTHROPIC"):
        monkeypatch.setenv(f"{name}_API_KEY", ENV_KEY_VALUE)
    monkeypatch.setenv("AZURE_ENDPOINT", "https://tenant.openai.azure.com")
    spy = Spy(registry_from_env(monkeypatch, MODEL_BASE_URL_ALLOWLIST=GATEWAY))

    result = startup(monkeypatch, spy, [agent_doc("x", factory_ia_model=provider, model="m", **fields)])

    assert agent_ids(result) == []
    # o destino é conferido antes do import da classe: vale também para o anthropic, de SDK não instalado
    assert failures(result)["x"]["reason"] == f"modelo do provider '{provider}' não aceita base_url"
    assert_nothing_leaked(result, ENV_KEY_VALUE, GATEWAY)


def test_params_hostis_na_chave_nao_sao_ecoados_no_log_nem_na_resposta(monkeypatch: pytest.MonkeyPatch, dns: FakeDns):
    spy = Spy(registry_from_env(monkeypatch, MODEL_BASE_URL_ALLOWLIST=GATEWAY))
    docs = [
        agent_doc(
            "chave-hostil", model="m1", base_url=GATEWAY_URL, model_params={"headers_" + PARAM_VALUE: 1}, **COMPAT
        ),
        agent_doc(
            "valor-aninhado", model="m2", base_url=GATEWAY_URL, model_params={"default_headers": {"x": PARAM_VALUE}}
        ),
    ]

    result = startup(monkeypatch, spy, docs)

    assert agent_ids(result) == []
    assert_nothing_leaked(result, PARAM_VALUE, "headers_")


# ── camelCase: ignorado, mas o registry real não recebe nem vaza ────────────────────────────────


def test_campos_novos_em_camel_case_sao_ignorados_com_aviso_e_o_agente_sobe_como_legado(
    monkeypatch: pytest.MonkeyPatch, dns: FakeDns
):
    monkeypatch.setenv("OPENAI_API_KEY", ENV_KEY_VALUE)
    spy = Spy(registry_from_env(monkeypatch, MODEL_BASE_URL_ALLOWLIST=GATEWAY))
    doc = agent_doc(
        "camel", factory_ia_model="openai", model="gpt-camel", baseUrl=f"https://{EVIL_HOST}/v1",
        apiKeyRef="env:QA_F202_GW_API_KEY", modelParams={"temperature": PARAM_VALUE},
    )

    result = startup(monkeypatch, spy, [doc])

    assert agent_ids(result) == ["camel"]
    model = spy.models["gpt-camel"]
    assert (model.base_url, model.api_key) == (None, ENV_KEY_VALUE)  # type: ignore[attr-defined]
    warnings = [r for r in result.logger.records if r.level == "warning"]
    camel_keys = [w.context["keys"] for w in warnings if "camelCase" in w.message]
    assert camel_keys == [["apiKeyRef", "baseUrl", "modelParams"]]
    assert dns.queries == []
    assert_nothing_leaked(result, ENV_KEY_VALUE, PARAM_VALUE, EVIL_HOST, "QA_F202_GW_API_KEY")


# ── (c) gather de agentes: sem estado cruzado, fora do event loop ───────────────────────────────


def test_criacao_concorrente_de_agentes_nao_mistura_params_nem_chaves_e_roda_fora_do_loop(
    monkeypatch: pytest.MonkeyPatch, dns: FakeDns
):
    total = 40
    for i in range(total):
        monkeypatch.setenv(f"QA_F202_N{i}_API_KEY", f"chave-{i}")
    spy = Spy(registry_from_env(monkeypatch, MODEL_BASE_URL_ALLOWLIST=GATEWAY))
    docs = [
        agent_doc(
            f"a{i}", model=f"m{i}", base_url=f"https://{GATEWAY}/v{i}", api_key_ref=f"env:QA_F202_N{i}_API_KEY",
            model_params={"temperature": i / 100, "max_tokens": 100 + i}, **COMPAT,
        )
        for i in range(total)
    ]

    result = startup(monkeypatch, spy, docs)

    assert agent_ids(result) == sorted(f"a{i}" for i in range(total))
    for i in range(total):
        model = spy.models[f"m{i}"]
        assert (model.temperature, model.max_tokens) == (i / 100, 100 + i)  # type: ignore[attr-defined]
        assert (model.api_key, model.base_url) == (f"chave-{i}", f"https://{GATEWAY}/v{i}")  # type: ignore[attr-defined]
    assert spy.on_event_loop == [False] * total
    assert len(dns.queries) == total and not any(on_loop for _, on_loop in dns.queries)
    assert all(f"chave-{i}" not in result.everything() for i in range(total))


def test_um_agente_recusado_no_gather_nao_afeta_os_vizinhos(monkeypatch: pytest.MonkeyPatch, dns: FakeDns):
    spy = Spy(registry_from_env(monkeypatch, MODEL_BASE_URL_ALLOWLIST=GATEWAY))
    docs = [
        agent_doc(f"a{i}", model=f"m{i}", base_url=GATEWAY_URL if i % 3 else f"https://{EVIL_HOST}/", **COMPAT)
        for i in range(30)
    ]

    result = startup(monkeypatch, spy, docs)

    assert agent_ids(result) == sorted(f"a{i}" for i in range(30) if i % 3)
    assert sorted(failures(result)) == sorted(f"a{i}" for i in range(30) if i % 3 == 0)


# ── teams ───────────────────────────────────────────────────────────────────────────────────────


def test_team_cria_o_modelo_real_com_campos_novos_fora_do_loop_e_recusa_destino_ruim(
    monkeypatch: pytest.MonkeyPatch, dns: FakeDns
):
    monkeypatch.setenv("QA_F202_GW_API_KEY", KEY_VALUE)
    spy = Spy(registry_from_env(monkeypatch, MODEL_BASE_URL_ALLOWLIST=GATEWAY))
    agents = [agent_doc("membro", model="m-membro", base_url=GATEWAY_URL, **COMPAT)]
    teams = [
        team_doc(
            "t-ok", ["membro"], model="m-team", base_url=GATEWAY_URL, api_key_ref="env:QA_F202_GW_API_KEY",
            model_params={"temperature": 0.5}, **COMPAT,
        ),
        team_doc("t-fora", ["membro"], model="m-fora", base_url=f"https://{EVIL_HOST}/v1", **COMPAT),
        team_doc("t-metadata", ["membro"], model="m-meta", base_url="http://169.254.169.254", **COMPAT),
    ]

    result = startup(monkeypatch, spy, agents, teams)

    assert team_ids(result) == ["t-ok"]
    team_model = spy.models["m-team"]
    assert (team_model.api_key, team_model.temperature, team_model.base_url) == (  # type: ignore[attr-defined]
        KEY_VALUE, 0.5, GATEWAY_URL
    )
    assert set(spy.models) == {"m-membro", "m-team"}
    assert spy.on_event_loop == [False] * len(spy.on_event_loop) and len(spy.on_event_loop) == 4
    bad = failures(result)
    assert "MODEL_BASE_URL_ALLOWLIST" in bad["t-fora"]["reason"] and "metadata" in bad["t-metadata"]["reason"]
    assert_nothing_leaked(result, KEY_VALUE, EVIL_HOST, "169.254.169.254")


# ── (d) modo dev local vs produção ──────────────────────────────────────────────────────────────

LOOPBACK_DOCS = [
    ("127.0.0.1", "http://127.0.0.1:8000/v1"),
    ("localhost", "http://localhost:8000/v1"),
    ("ipv6", "http://[::1]:8000/v1"),
]


@pytest.mark.parametrize(("name", "url"), LOOPBACK_DOCS, ids=[n for n, _ in LOOPBACK_DOCS])
def test_mesmo_documento_com_loopback_sobe_no_dev_local_e_e_recusado_fora_dele(
    monkeypatch: pytest.MonkeyPatch, dns: FakeDns, name: str, url: str
):
    dns.answers["localhost"] = "127.0.0.1"
    doc = agent_doc("local", model="m-local", base_url=url, **COMPAT)

    dev = startup(monkeypatch, Spy(registry_from_env(monkeypatch)), [doc])
    prod_spy = Spy(
        registry_from_env(
            monkeypatch, ENVIRONMENT="production", API_KEY_RUN="r" * 32, API_KEY_ADMIN="a" * 32
        )
    )
    prod = startup(monkeypatch, prod_spy, [doc])

    assert agent_ids(dev) == ["local"]
    assert agent_ids(prod) == [] and "MODEL_BASE_URL_ALLOWLIST" in failures(prod)["local"]["reason"]
    assert "127.0.0.1" not in prod.everything() and "localhost" not in prod.everything()


def test_dev_com_bind_publico_nao_e_modo_dev_local_e_recusa_loopback(monkeypatch: pytest.MonkeyPatch, dns: FakeDns):
    """Sem chaves + APP_HOST fora de loopback não é o modo dev local da borda: a regra é a mesma."""
    registry = registry_from_env(monkeypatch, APP_HOST="0.0.0.0")  # noqa: S104 - bind público é o caso testado

    result = startup(
        monkeypatch, Spy(registry), [agent_doc("local", model="m", base_url="http://127.0.0.1:8000/v1", **COMPAT)]
    )

    assert agent_ids(result) == []


# ── (e) OLLAMA_BASE_URL no cliente real ─────────────────────────────────────────────────────────


def test_ollama_base_url_do_operador_chega_ao_cliente_real_do_ollama_e_nao_passa_pela_allowlist(
    monkeypatch: pytest.MonkeyPatch, dns: FakeDns
):
    spy = Spy(registry_from_env(monkeypatch, OLLAMA_BASE_URL="http://ollama-interno:11434"))

    result = startup(monkeypatch, spy, [agent_doc("o", model="llama-x")])

    assert agent_ids(result) == ["o"]
    client = spy.models["llama-x"].get_client()  # type: ignore[attr-defined]
    assert "ollama-interno" in str(client._client.base_url)
    assert client._client.follow_redirects is True  # padrão do SDK: a base_url é do operador
    assert dns.queries == []  # URL do operador é confiável: sem consulta DNS


def test_base_url_do_documento_para_o_ollama_desliga_redirect_no_cliente_real(
    monkeypatch: pytest.MonkeyPatch, dns: FakeDns
):
    spy = Spy(
        registry_from_env(monkeypatch, OLLAMA_BASE_URL="http://ollama-interno:11434", MODEL_BASE_URL_ALLOWLIST=GATEWAY)
    )

    result = startup(
        monkeypatch, spy, [agent_doc("o", model="llama-y", factory_ia_model="ollama", base_url=f"http://{GATEWAY}:11434")]
    )

    assert agent_ids(result) == ["o"]
    client = spy.models["llama-y"].get_client()  # type: ignore[attr-defined]
    assert GATEWAY in str(client._client.base_url) and client._client.follow_redirects is False


# ── RAG semântico: embedder pelo registry real ──────────────────────────────────────────────────


def test_rag_semantico_cria_o_embedder_real_com_campos_novos_fora_do_loop(
    monkeypatch: pytest.MonkeyPatch, dns: FakeDns
):
    monkeypatch.setenv("QA_F202_EMB_API_KEY", KEY_VALUE)
    spy = Spy(registry_from_env(monkeypatch, MODEL_BASE_URL_ALLOWLIST=GATEWAY, OLLAMA_BASE_URL="http://ollama-interno:11434"))
    docs = [
        agent_doc(
            "rag-gw", model="m-chat",
            rag_config={
                "active": True, "factory_ia_model": "openai_compatible", "model": "bge-m3", "base_url": GATEWAY_URL,
                "api_key_ref": "env:QA_F202_EMB_API_KEY", "model_params": {"dimensions": 64},
            },
        ),
        agent_doc("rag-padrao", model="m-chat2", rag_config={"active": True}),  # ollama / nomic do operador
    ]

    result = startup(monkeypatch, spy, docs)

    assert agent_ids(result) == ["rag-gw", "rag-padrao"]
    gw = spy.embedders["bge-m3"]
    assert type(gw).__qualname__ == "OpenAILikeEmbedder"
    assert (gw.api_key, gw.base_url, gw.dimensions) == (KEY_VALUE, GATEWAY_URL, 64)  # type: ignore[attr-defined]
    default = spy.embedders["nomic-embed-text:latest"]
    assert type(default).__qualname__ == "OllamaEmbedder" and default.host == "http://ollama-interno:11434"  # type: ignore[attr-defined]
    assert not any(spy.on_event_loop)
    assert_nothing_leaked(result, KEY_VALUE, GATEWAY, "QA_F202_EMB_API_KEY")


@pytest.mark.parametrize(
    "rag",
    [
        {"base_url": f"https://{EVIL_HOST}/v1", "api_key_ref": "env:QA_F202_EMB_API_KEY"},
        {"base_url": "http://169.254.169.254/latest"},
        {"base_url": GATEWAY_URL, "model_params": {"temperature": 0.1}},
        {"base_url": GATEWAY_URL, "api_key_ref": "env:QA_F202_AUSENTE_API_KEY"},
    ],
    ids=["fora-da-allowlist", "metadata", "param-de-chat-no-embedder", "ref-ausente"],
)
def test_rag_com_embedder_recusado_sobe_o_agente_sem_rag_e_loga_o_motivo_sem_vazar(
    monkeypatch: pytest.MonkeyPatch, dns: FakeDns, rag: dict[str, Any]
):
    monkeypatch.setenv("QA_F202_EMB_API_KEY", KEY_VALUE)
    spy = Spy(registry_from_env(monkeypatch, MODEL_BASE_URL_ALLOWLIST=GATEWAY))
    doc = agent_doc(
        "rag-ruim", model="m-chat",
        rag_config={"active": True, "factory_ia_model": "openai_compatible", "model": "bge-m3", **rag},
    )

    result = startup(monkeypatch, spy, [doc])

    assert agent_ids(result) == ["rag-ruim"]  # o agente sobe, sem o RAG
    assert "bge-m3" not in spy.embedders
    [warning] = [r for r in result.logger.records if r.message == "Erro ao criar RAG"]
    assert warning.context["error_type"] == "InvalidModelConfigError" and warning.context["reason"]
    assert_nothing_leaked(result, KEY_VALUE, EVIL_HOST, "169.254.169.254", "QA_F202_EMB_API_KEY")


# ── indexação hierárquica: embedder pelo registry real (spec fake, sem SDK) ──────────────────────


class _Summary(ISummaryGenerator):
    async def generate_summary(self, content: str) -> str:
        return content[:20]


def _indexing(registry: ProviderRegistry) -> DocumentIndexingService:
    return DocumentIndexingService(
        parser=TextDocumentParser(),
        tree_repository=InMemoryDocumentTreeRepository(),
        summary_generator=_Summary(),
        embedder_factory=registry,
        logger=RecordingLogger(),
    )


async def test_indexacao_cria_o_embedder_pelo_registry_fora_do_loop_e_consulta_dns_fora_do_loop():
    queries: list[tuple[str, bool]] = []

    def resolver(host: str) -> list[str]:
        queries.append((host, running_on_event_loop()))
        return ["10.20.30.40"]

    spec = ProviderSpec(
        id="acme", sdk_package="acme-sdk", embedder=ClassSpec(class_path=EMBEDDER_PATH, base_url_kwarg="host")
    )
    registry = ProviderRegistry([spec], policy=DestinationPolicy(allowlist=(GATEWAY,), resolver=resolver))
    rag = RagConfig(
        active=True, factory_ia_model="acme", model="emb-1", base_url=GATEWAY_URL,
        search_strategy=SearchStrategy.HIERARCHICAL,
    )

    nodes = await _indexing(registry).index_document("doc.md", "# Titulo\n\nTexto.\n\n## Secao\n\nOutro texto.\n", rag)

    assert nodes and all(node.embedding for node in nodes)
    assert queries == [(GATEWAY, False)]


async def test_indexacao_com_destino_recusado_levanta_erro_nosso_sem_o_host():
    spec = ProviderSpec(
        id="acme", sdk_package="acme-sdk", embedder=ClassSpec(class_path=EMBEDDER_PATH, base_url_kwarg="host")
    )
    policy = DestinationPolicy(allowlist=(GATEWAY,), resolver=lambda host: ["10.0.0.1"])
    registry = ProviderRegistry([spec], policy=policy)
    rag = RagConfig(
        active=True, factory_ia_model="acme", model="emb-1", base_url=f"https://{EVIL_HOST}/v1",
        api_key_ref="env:QA_F202_LEAK_API_KEY", search_strategy=SearchStrategy.HIERARCHICAL,
    )

    with pytest.raises(InvalidModelConfigError, match="MODEL_BASE_URL_ALLOWLIST") as caught:
        await _indexing(registry).index_document("doc.md", "# Titulo\n\nTexto.\n", rag)

    assert EVIL_HOST not in str(caught.value) and "QA_F202_LEAK" not in str(caught.value)
