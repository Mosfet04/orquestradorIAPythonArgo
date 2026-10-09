"""QA F2-03: plugin de provider no startup real (``AppFactory`` -> ``DependencyContainer`` real).

Só o Mongo (cliente motor e coleções) e a rede (DNS) são falsos. Critérios:
(a) plugin liberado em ``PLUGIN_ALLOWLIST`` é **usável**: o documento de agente com
    ``factoryIaModel`` do plugin vira agente em ``/agents`` com o modelo real do agno, criado pelo
    mesmo ``ProviderRegistry`` dos built-ins (destino e chave do F2-02), sem vazar a chave;
(b) plugin instalado e não liberado nunca é importado e o documento dele é isolado;
(c) startup recusado (duplicata, plugin ausente, import dinâmico fora do modo dev local) não abre
    cliente Mongo, não lê documentos e não abre o socket (uvicorn real);
(d) o carregamento roda fora do event loop; envs hostis falham sem ecoar o valor.
"""

from __future__ import annotations

import socket
import sys
import threading
import time
from collections import defaultdict
from collections.abc import Iterator
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any, ClassVar

import pytest
import uvicorn
from agno.models.openai.like import OpenAILike
from starlette.testclient import TestClient

from src.application.services import agent_factory_service, team_factory_service
from src.infrastructure import dependency_injection as di
from src.infrastructure.providers.plugins import ENTRY_POINT_GROUP, PluginLoadError
from src.infrastructure.repositories import mongo_base
from src.infrastructure.web import app_factory
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import FakeMongoClient, FakeMongoCollection, RecordingLogger
from tests.fakes.plugins import FakeSite

pytestmark = pytest.mark.usefixtures("offline_knowledge")

KEYS = {"API_KEY_RUN": "r" * 32, "API_KEY_ADMIN": "a" * 32}
LOCAL = {"base_url": "http://127.0.0.1:7777", "client": ("127.0.0.1", 50000)}
PLUGIN_KEY = "sk-QA-F203-CHAVE-DO-PLUGIN-NAO-PODE-VAZAR"
ENV_SECRET = "SEGREDO-QA-F203-NA-ENV"  # noqa: S105 - marcador de vazamento
EVIL_HOST = "coletor.evil.invalid"
PROVIDER_HOST = "api.acmeai.example"
_ENV = (
    "PLUGIN_ALLOWLIST", "ALLOW_DYNAMIC_IMPORT", "DYNAMIC_PROVIDER_SPECS", "ENVIRONMENT", "APP_HOST",
    "API_KEY_RUN", "API_KEY_ADMIN", "MODEL_BASE_URL_ALLOWLIST", "SECRETS_DIR", "OPENAI_API_KEY",
    "ACMEAI_API_KEY", "OLLAMA_BASE_URL", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS",
)

# Plugin com a classe REAL do agno (OpenAILike): prova o modelo exposto, não um fake.
PLUGIN_MODULE = """
from src.infrastructure.providers import ClassSpec, ProviderSpec

SPEC = ProviderSpec(
    id="acmeai",
    sdk_package="openai",
    aliases=("acme-ai",),
    chat=ClassSpec(
        class_path="agno.models.openai.like.OpenAILike",
        params={"temperature": "temperature", "max_tokens": "max_tokens"},
        base_url_kwarg="base_url",
    ),
    default_hosts=frozenset({"api.acmeai.example"}),
    api_key_env="ACMEAI_API_KEY",
)
"""


class FakeMotor:
    """Cliente motor falso do composition root: ping ok, ``close`` registrado."""

    instances: ClassVar[list[FakeMotor]] = []

    def __init__(self, *_: object, **__: object) -> None:
        self.closed = False
        self.admin = self
        FakeMotor.instances.append(self)

    async def command(self, *_: object) -> dict[str, int]:
        return {"ok": 1}

    def close(self) -> None:
        self.closed = True


class _Collection(FakeMongoCollection):
    async def create_index(self, *_: object, **__: object) -> str:
        return "idx"


class Booted:
    """Resultado de um startup real."""

    def __init__(self) -> None:
        self.status = 0
        self.agents: Any = None
        self.config: Any = None
        self.created: list[Any] = []
        self.logger = RecordingLogger()
        self.agent_collection = _Collection()

    def ids(self) -> list[str]:
        return sorted(a["id"] for a in self.agents) if isinstance(self.agents, list) else []

    def log_blob(self) -> str:
        return repr(self.logger.records) + repr(self.agents) + repr(self.config)

    def failures(self) -> dict[str, dict[str, Any]]:
        return {
            r.context["agent_id"]: r.context
            for r in self.logger.records
            if r.level == "error" and r.message.endswith("não carregado")
        }


def _agent(agent_id: str, **extra: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "id": agent_id, "nome": f"Agente {agent_id}", "model": "acme-1", "descricao": "d", "prompt": "p",
        "tools_ids": [], "active": True,
    }
    doc.update(extra)
    return doc


@pytest.fixture(autouse=True)
def no_state_left_behind(tmp_path: Path) -> Iterator[None]:
    """Vazamento entre testes (xdist): módulo ou entry point do site falso depois do teardown.

    Autouse e declarada antes: o teardown dela roda por último, depois do da ``plugin_site``.
    """
    path_before = list(sys.path)
    yield
    root = str(tmp_path)
    leaked = [
        name
        for name, module in list(sys.modules.items())
        if str(getattr(module, "__file__", None) or "").startswith(root)
    ]
    assert leaked == []
    assert sys.path == path_before
    lingering = [
        ep for ep in entry_points(group=ENTRY_POINT_GROUP) if str(getattr(ep.dist, "_path", "")).startswith(root)
    ]
    assert lingering == []


@pytest.fixture
def site(plugin_site: FakeSite, monkeypatch: pytest.MonkeyPatch) -> FakeSite:
    plugin_site.install(
        "Acme_AI.Plugin", {"acmeai": "qa_f203_acme_mod:SPEC"}, {"qa_f203_acme_mod": PLUGIN_MODULE}, version="2.0.1"
    )
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    monkeypatch.setenv("OTEL_ENABLED", "false")
    FakeMotor.instances.clear()
    return plugin_site


@pytest.fixture
def dns(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    queries: list[str] = []

    def getaddrinfo(host: str, *_: Any, **__: Any) -> list[tuple[Any, ...]]:
        queries.append(host)
        return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("203.0.113.9", 0))]

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    return queries


def _install_fakes(monkeypatch: pytest.MonkeyPatch, result: Booted, docs: list[dict[str, Any]]) -> None:
    result.agent_collection.docs = list(docs)
    collections: defaultdict[str, FakeMongoCollection] = defaultdict(_Collection)  # tools, árvore...
    collections["agents_config"] = result.agent_collection
    client = FakeMongoClient(collections)
    monkeypatch.setattr(mongo_base.MongoClientFactory, "get_client", classmethod(lambda cls, _conn: client))
    monkeypatch.setattr(di, "AsyncIOMotorClient", FakeMotor)
    monkeypatch.setattr(di, "StructlogLoggerAdapter", lambda _name: result.logger)
    monkeypatch.setattr(agent_factory_service, "MongoAgentDb", lambda **_: None)
    monkeypatch.setattr(team_factory_service, "MongoAgentDb", lambda **_: None)
    monkeypatch.setattr(app_factory, "setup_telemetry", lambda config: None)
    monkeypatch.setattr(app_factory, "shutdown_telemetry", lambda: None)
    original = di.ProviderRegistry.create_model

    def recording(self: Any, config: Any) -> Any:
        model = original(self, config)
        result.created.append(model)
        return model

    monkeypatch.setattr(di.ProviderRegistry, "create_model", recording)


def boot_real(monkeypatch: pytest.MonkeyPatch, docs: list[dict[str, Any]]) -> Booted:
    """Startup real: ``AppFactory`` + ``DependencyContainer`` + repositórios sobre coleção fake."""
    result = Booted()
    _install_fakes(monkeypatch, result, docs)
    app = AppFactory().create_app()
    with TestClient(app, **LOCAL) as http:  # type: ignore[arg-type]
        response = http.get("/agents")
        result.status = response.status_code
        result.agents = response.json()
        result.config = http.get("/config").json()
    return result


def refused_startup(monkeypatch: pytest.MonkeyPatch, docs: list[dict[str, Any]]) -> tuple[Booted, PluginLoadError]:
    result = Booted()
    _install_fakes(monkeypatch, result, docs)
    app = AppFactory().create_app()
    with pytest.raises(PluginLoadError) as caught:
        with TestClient(app, **LOCAL):  # type: ignore[arg-type]
            pass  # pragma: no cover - o startup não pode completar
    return result, caught.value


# ── (a) plugin liberado é usável no startup real ────────────────────────────────────────────────


def test_plugin_liberado_sobe_no_startup_real_e_o_modelo_dele_e_criado_e_exposto(
    site: FakeSite, dns: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLUGIN_ALLOWLIST", "Acme-AI.plugin:acmeai")
    monkeypatch.setenv("ACMEAI_API_KEY", PLUGIN_KEY)
    docs = [
        _agent("do-plugin", factoryIaModel="acmeai", model="acme-1", model_params={"temperature": 0.4}),
        _agent("alias-maiusculo", factory_ia_model="ACME-AI", model="acme-2", model_params={"max_tokens": 77}),
        _agent("legado", model="llama-legado"),  # ollama, como sempre
    ]

    result = boot_real(monkeypatch, docs)

    assert result.status == 200
    assert result.ids() == ["alias-maiusculo", "do-plugin", "legado"]
    exposed = {a["id"]: a["model"] for a in result.agents}
    assert exposed["do-plugin"] == {"name": "OpenAILike", "model": "acme-1", "provider": "OpenAI"}
    by_id = {m.id: m for m in result.created}
    assert type(by_id["acme-1"]) is OpenAILike
    assert (by_id["acme-1"].api_key, by_id["acme-1"].temperature, by_id["acme-1"].base_url) == (PLUGIN_KEY, 0.4, None)
    assert (by_id["acme-2"].api_key, by_id["acme-2"].max_tokens) == (PLUGIN_KEY, 77)
    assert result.failures() == {}
    [loaded] = [r for r in result.logger.records if r.message == "Plugin de provider carregado"]
    assert (loaded.level, loaded.context) == (
        "info",
        {"distribution": "Acme_AI.Plugin", "version": "2.0.1", "entry_point": "acmeai", "provider_id": "acmeai"},
    )
    assert PLUGIN_KEY not in result.log_blob() and "ACMEAI_API_KEY" not in result.log_blob()
    assert [m.closed for m in FakeMotor.instances] == [True]  # shutdown fecha o cliente


def test_plugin_sem_chave_no_ambiente_isola_so_o_documento_dele(
    site: FakeSite, dns: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLUGIN_ALLOWLIST", "acme-ai-plugin:acmeai")

    result = boot_real(monkeypatch, [_agent("do-plugin", factory_ia_model="acmeai"), _agent("legado")])

    assert result.ids() == ["legado"]
    assert result.failures()["do-plugin"]["reason"] == "ACMEAI_API_KEY não configurado"


def test_destino_e_chave_do_plugin_seguem_a_guarda_do_f2_02_no_startup(
    site: FakeSite, dns: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLUGIN_ALLOWLIST", "acme-ai-plugin:acmeai")
    monkeypatch.setenv("ACMEAI_API_KEY", PLUGIN_KEY)
    https_provider = f"https://{PROVIDER_HOST}/v1"
    docs = [
        _agent(
            "ok", model="m-ok", factory_ia_model="acmeai", base_url=https_provider, api_key_ref="env:ACMEAI_API_KEY"
        ),
        _agent("fora", model="m-fora", factory_ia_model="acmeai", base_url=f"https://{EVIL_HOST}/v1",
               api_key_ref="env:ACMEAI_API_KEY"),
        _agent("fora-sem-ref", model="m-fora2", factory_ia_model="acmeai", base_url=f"https://{EVIL_HOST}/v1"),
        _agent("metadata", model="m-meta", factory_ia_model="acmeai", base_url="http://169.254.169.254/latest"),
        _agent("http-no-provider", model="m-http", factory_ia_model="acmeai", base_url=f"http://{PROVIDER_HOST}/v1"),
        _agent("loopback", model="m-lo", factory_ia_model="acmeai", base_url="http://127.0.0.1:9/v1"),
        _agent(
            "param-fora-da-allowlist", model="m-param", factory_ia_model="acmeai", model_params={"headers": ENV_SECRET}
        ),
        _agent("depois", model="m-depois", factory_ia_model="acmeai", base_url=https_provider),
    ]

    result = boot_real(monkeypatch, docs)

    # loopback entra: este startup é modo dev local (sem chaves, 127.0.0.1, development)
    assert result.ids() == ["depois", "loopback", "ok"]
    failures = result.failures()
    assert {k: v["error_type"] for k, v in failures.items()} == dict.fromkeys(
        ("fora", "fora-sem-ref", "metadata", "http-no-provider", "param-fora-da-allowlist"), "InvalidModelConfigError"
    )
    assert "MODEL_BASE_URL_ALLOWLIST" in failures["fora"]["reason"]
    assert "metadata" in failures["metadata"]["reason"] and "https" in failures["http-no-provider"]["reason"]
    assert "model_params" in failures["param-fora-da-allowlist"]["reason"]
    assert {m.id: m.base_url for m in result.created} == {
        "m-ok": https_provider, "m-lo": "http://127.0.0.1:9/v1", "m-depois": https_provider,
    }
    assert dns.count(PROVIDER_HOST) == 2  # "ok" e "depois"; a recusa não depende de consulta ao host de fora
    for forbidden in (PLUGIN_KEY, ENV_SECRET, EVIL_HOST, "169.254.169.254"):
        assert forbidden not in result.log_blob(), forbidden


def test_fora_do_modo_dev_local_o_plugin_nao_ganha_loopback(
    site: FakeSite, dns: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLUGIN_ALLOWLIST", "acme-ai-plugin:acmeai")
    monkeypatch.setenv("ACMEAI_API_KEY", PLUGIN_KEY)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("API_KEY_RUN", "r" * 32)
    monkeypatch.setenv("API_KEY_ADMIN", "a" * 32)
    result = Booted()
    _install_fakes(monkeypatch, result, [_agent("loopback", factory_ia_model="acmeai", base_url="http://127.0.0.1:9/v1")])
    app = AppFactory().create_app()
    headers = {"Authorization": f"Bearer {'a' * 32}"}

    with TestClient(app, **LOCAL, headers=headers) as http:  # type: ignore[arg-type]
        listed = http.get("/agents")

    assert listed.status_code == 404  # sem agente válido o AgentOS não monta a rota
    assert result.created == []
    assert "MODEL_BASE_URL_ALLOWLIST" in result.failures()["loopback"]["reason"]


# ── (b) plugin não liberado nunca é importado ───────────────────────────────────────────────────


def test_plugin_instalado_e_nao_liberado_nunca_e_importado_e_o_documento_dele_e_isolado(
    site: FakeSite, dns: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    site.install(
        "evil-plugin",
        {"evil": "qa_f203_evil_mod:SPEC"},
        {"qa_f203_evil_mod": site.side_effect_module("qa_f203_evil_mod", "evil")},
    )
    monkeypatch.setenv("PLUGIN_ALLOWLIST", "acme-ai-plugin:acmeai")
    monkeypatch.setenv("ACMEAI_API_KEY", PLUGIN_KEY)

    result = boot_real(
        monkeypatch, [_agent("bom", factory_ia_model="acmeai"), _agent("mau", factory_ia_model="evil", model="x")]
    )

    assert result.ids() == ["bom"]
    assert not site.imported("qa_f203_evil_mod")
    assert "não suportado" in result.failures()["mau"]["reason"]
    [ignored] = [r for r in result.logger.records if "fora da PLUGIN_ALLOWLIST" in r.message]
    assert (ignored.level, ignored.context) == ("warning", {"distribution": "evil-plugin", "entry_point": "evil"})


def test_sem_allowlist_o_plugin_instalado_nao_e_importado_e_o_app_sobe(
    site: FakeSite, dns: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    module = site.side_effect_module("qa_f203_m_mod", "m")
    site.install("marcador", {"m": "qa_f203_m_mod:SPEC"}, {"qa_f203_m_mod": module})

    result = boot_real(monkeypatch, [_agent("legado"), _agent("acme", factory_ia_model="acmeai")])

    assert result.ids() == ["legado"]
    assert not site.imported("qa_f203_m_mod") and not site.imported("qa_f203_acme_mod")


# ── (c) startup recusado: antes do Mongo e do bind ──────────────────────────────────────────────


def _assert_nothing_opened(result: Booted) -> None:
    assert FakeMotor.instances == []  # nenhum cliente Mongo aberto
    assert result.agent_collection.queries == []  # nenhum documento lido


def test_duplicata_com_built_in_recusa_o_startup_com_mensagem_clara_antes_do_mongo(
    site: FakeSite, monkeypatch: pytest.MonkeyPatch
) -> None:
    site.install(
        "clash-plugin", {"clash": "qa_f203_clash_mod:SPEC"},
        {"qa_f203_clash_mod": PLUGIN_MODULE.replace('id="acmeai"', 'id="OpenAI"')},
    )
    monkeypatch.setenv("PLUGIN_ALLOWLIST", "clash-plugin:clash")

    result, error = refused_startup(monkeypatch, [_agent("a")])

    assert str(error) == (
        "plugin 'clash-plugin:clash': provider 'OpenAI' com id ou alias já registrado (openai: built-in); "
        "id e aliases de provider são únicos, sem diferenciar caixa"
    )
    _assert_nothing_opened(result)


def test_plugin_da_allowlist_ausente_recusa_o_startup_sem_importar_os_presentes(
    site: FakeSite, monkeypatch: pytest.MonkeyPatch
) -> None:
    module = site.side_effect_module("qa_f203_m_mod", "m")
    site.install("marca-plugin", {"m": "qa_f203_m_mod:SPEC"}, {"qa_f203_m_mod": module})
    monkeypatch.setenv("PLUGIN_ALLOWLIST", f"marca-plugin:m, {ENV_SECRET}-plugin:falta")

    result, error = refused_startup(monkeypatch, [_agent("a")])

    assert str(error) == (
        "PLUGIN_ALLOWLIST: entrada 2 (contando da esquerda as não vazias) sem plugin instalado no grupo "
        "orquestrador.providers com nome nos metadados da distribuição; instale a distribuição ou remova a entrada"
    )
    assert not site.imported("qa_f203_m_mod")
    _assert_nothing_opened(result)


@pytest.mark.parametrize(
    "env",
    [
        {"ALLOW_DYNAMIC_IMPORT": "true", **KEYS},
        {"ALLOW_DYNAMIC_IMPORT": "true", "APP_HOST": "0.0.0.0", "API_KEY_RUN": "r" * 32,  # noqa: S104
         "API_KEY_ADMIN": "a" * 32},  # bind público (exige chaves) não é modo dev local
        {"ALLOW_DYNAMIC_IMPORT": "true", "ENVIRONMENT": "production", **KEYS},
        {},
    ],
    ids=["com-chaves", "bind-publico", "producao", "sem-o-flag"],
)
def test_import_dinamico_fora_do_dev_local_ou_sem_flag_recusa_o_startup_sem_importar(
    site: FakeSite, monkeypatch: pytest.MonkeyPatch, env: dict[str, str]
) -> None:
    monkeypatch.setenv("DYNAMIC_PROVIDER_SPECS", "qa_f203_acme_mod:SPEC")
    for name, value in env.items():
        monkeypatch.setenv(name, value)

    result, error = refused_startup(monkeypatch, [_agent("a")])

    assert "ALLOW_DYNAMIC_IMPORT=true" in str(error) and "qa_f203_acme_mod" not in str(error)
    assert not site.imported("qa_f203_acme_mod")
    _assert_nothing_opened(result)


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="BUG-F2-03-QA-2 (pré-existente ao F2-03): falha em _initialize depois de abrir o AsyncIOMotorClient "
    "deixa o cliente aberto, porque o lifespan só chama cleanup() se o container foi atribuído",
)
def test_falha_tardia_no_wiring_nao_deixa_cliente_mongo_aberto(
    site: FakeSite, monkeypatch: pytest.MonkeyPatch
) -> None:
    result = Booted()
    _install_fakes(monkeypatch, result, [_agent("a")])

    def broken(**_: object) -> None:
        raise RuntimeError("wiring quebrado")

    monkeypatch.setattr(di, "MongoToolRepository", broken)
    app = AppFactory().create_app()

    with pytest.raises(RuntimeError, match="wiring quebrado"):
        with TestClient(app, **LOCAL):  # type: ignore[arg-type]
            pass  # pragma: no cover

    assert FakeMotor.instances and all(client.closed for client in FakeMotor.instances)


def test_import_dinamico_no_dev_local_com_o_flag_e_usavel_e_auditado(
    site: FakeSite, dns: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALLOW_DYNAMIC_IMPORT", "true")
    monkeypatch.setenv("DYNAMIC_PROVIDER_SPECS", "qa_f203_acme_mod:SPEC")
    monkeypatch.setenv("ACMEAI_API_KEY", PLUGIN_KEY)

    result = boot_real(monkeypatch, [_agent("dinamico", factory_ia_model="acmeai")])

    assert result.ids() == ["dinamico"]
    [audit] = [r for r in result.logger.records if "import dinâmico" in r.message]
    assert (audit.level, audit.context) == (
        "warning", {"entry": 1, "target": "qa_f203_acme_mod:SPEC", "provider_id": "acmeai"}
    )
    assert PLUGIN_KEY not in result.log_blob()


def test_flag_de_import_dinamico_sozinho_nao_importa_nem_registra_nada(
    site: FakeSite, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("ALLOW_DYNAMIC_IMPORT", "true")

    result = boot_real(monkeypatch, [_agent("acme", factory_ia_model="acmeai"), _agent("legado")])

    assert result.ids() == ["legado"]
    assert not site.imported("qa_f203_acme_mod")


def _bound_socket() -> tuple[socket.socket, int]:
    """Socket ligado a uma porta efêmera e entregue ainda aberto ao uvicorn (``sockets=[...]``).

    Sem a janela entre "achar porta livre" e o bind do servidor, em que outro worker do
    pytest-xdist poderia tomar a porta (mesmo padrão de ``test_qa_f1_04_auth_stack._serve``).
    Ligado e sem ``listen`` o socket recusa conexão: quem chama ``listen`` é o ``create_server``
    do uvicorn, depois do startup do lifespan.
    """
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    return listener, int(listener.getsockname()[1])


def _connects(port: int) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=0.5).close()
    except OSError:
        return False
    return True


def test_uvicorn_real_nao_abre_o_socket_quando_o_startup_e_recusado(
    site: FakeSite, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLUGIN_ALLOWLIST", "nao-instalado-plugin:x")
    result = Booted()
    _install_fakes(monkeypatch, result, [_agent("a")])
    listener, port = _bound_socket()
    config = uvicorn.Config(
        AppFactory().create_app(), host="127.0.0.1", port=port, log_level="critical", log_config=None
    )
    server = uvicorn.Server(config)
    listening: list[bool] = []
    original = server.startup

    async def spy_startup(sockets: Any = None) -> None:
        try:
            await original(sockets=sockets)
        finally:
            listening.append(_connects(port))

    monkeypatch.setattr(server, "startup", spy_startup)

    try:
        with pytest.raises(SystemExit) as caught:
            server.run(sockets=[listener])
        # Controle: o socket segue ligado (porta nossa) e, sem listen, recusa conexão.
        assert listener.getsockname()[1] == port
        assert listening == [False] and not _connects(port)
    finally:
        listener.close()

    assert caught.value.code == 3  # uvicorn STARTUP_FAILURE
    _assert_nothing_opened(result)


def test_uvicorn_real_abre_o_socket_com_o_plugin_liberado_controle_positivo(
    site: FakeSite, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PLUGIN_ALLOWLIST", "acme-ai-plugin:acmeai")
    monkeypatch.setenv("ACMEAI_API_KEY", PLUGIN_KEY)
    result = Booted()
    _install_fakes(monkeypatch, result, [_agent("a", factory_ia_model="acmeai")])
    listener, port = _bound_socket()
    config = uvicorn.Config(
        AppFactory().create_app(), host="127.0.0.1", port=port, log_level="critical", log_config=None
    )
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [listener]}, daemon=True)
    thread.start()
    try:
        deadline = time.monotonic() + 20
        while not server.started and time.monotonic() < deadline:
            time.sleep(0.05)
        assert server.started and _connects(port)
    finally:
        server.should_exit = True
        thread.join(timeout=20)
        listener.close()
    assert not thread.is_alive()


# ── (d) fora do event loop; envs hostis ─────────────────────────────────────────────────────────


def test_plugin_e_importado_fora_do_event_loop(site: FakeSite, dns: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    probe = site.root / "loop.txt"
    site.install(
        "thread-plugin", {"th": "qa_f203_thread_mod:SPEC"},
        {"qa_f203_thread_mod": (
            "from tests.fakes.models import running_on_event_loop\n"
            f"open({str(probe)!r}, 'w').write(str(running_on_event_loop()))\n"
            + PLUGIN_MODULE.replace('id="acmeai"', 'id="th"').replace('aliases=("acme-ai",)', "aliases=()")
        )},
    )
    monkeypatch.setenv("PLUGIN_ALLOWLIST", "thread-plugin:th")

    boot_real(monkeypatch, [_agent("legado")])

    assert probe.read_text() == "False"


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("PLUGIN_ALLOWLIST", ENV_SECRET),
        ("PLUGIN_ALLOWLIST", f"ok-plugin:ok,{ENV_SECRET}:x:y"),
        ("PLUGIN_ALLOWLIST", f"https://user:{ENV_SECRET}@host/x:y"),
        ("PLUGIN_ALLOWLIST", f"dist:{ENV_SECRET}/../../etc"),
        ("PLUGIN_ALLOWLIST", f"{'a' * 5000}{ENV_SECRET}"),
        ("DYNAMIC_PROVIDER_SPECS", ENV_SECRET),
        ("DYNAMIC_PROVIDER_SPECS", f"os:system('{ENV_SECRET}')"),
        ("DYNAMIC_PROVIDER_SPECS", f"pasta/{ENV_SECRET}.py:SPEC"),
        ("DYNAMIC_PROVIDER_SPECS", f"mod:SPEC [{ENV_SECRET}]"),
        ("ALLOW_DYNAMIC_IMPORT", ENV_SECRET),
        ("ENABLE_DOCS", ENV_SECRET),
    ],
    ids=lambda v: v[:24] if isinstance(v, str) else str(v),
)
def test_env_hostil_falha_o_startup_sem_importar_nada_nem_ecoar_o_valor(
    site: FakeSite, monkeypatch: pytest.MonkeyPatch, name: str, value: str
) -> None:
    monkeypatch.setenv(name, value)

    with pytest.raises(ValueError) as caught:
        AppFactory().create_app()  # AppConfig.load() no create_app: falha antes do bind

    assert name in str(caught.value)
    assert ENV_SECRET not in str(caught.value) and ENV_SECRET not in repr(caught.value)
    assert not site.imported("qa_f203_acme_mod")
