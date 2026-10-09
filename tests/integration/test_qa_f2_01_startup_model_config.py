"""QA F2-01: startup real (AppFactory + repositórios Mongo reais sobre coleção fake) com
documentos legados, com os campos novos e com campos novos hostis.

Critérios: (a) documentos antigos sobem iguais (respostas HTTP e provider/modelo pedidos às
fábricas de modelo/embedder idênticos) com e sem ``model_params``/``base_url``/``api_key_ref``;
desde a F2-02 a criação usa os campos novos, então eles chegam à fábrica junto (antes a premissa
era "a criação ainda não os usa"); (b) documento com campo novo inválido é isolado e o app sobe
com os demais; (c) nenhum valor de segredo/URL com credencial chega a log nem a resposta HTTP,
inclusive com o ``ProviderRegistry`` real recusando o destino; (d) a indexação hierárquica
mantém o fallback de embedder (ollama / nomic-embed-text).
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient

from src.application.services import agent_factory_service, team_factory_service
from src.application.services.agent_factory_service import AgentFactoryService
from src.application.services.document_indexing_service import DocumentIndexingService
from src.application.services.team_factory_service import TeamFactoryService
from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase
from src.application.use_cases.get_active_teams_use_case import GetActiveTeamsUseCase
from src.domain.entities.rag_config import RagConfig, SearchStrategy
from src.domain.ports import IEmbedderFactory, IModelFactory
from src.domain.ports.summary_generator_port import ISummaryGenerator
from src.infrastructure.http.http_tool_factory import HttpToolFactory
from src.infrastructure.parsers.text_document_parser import TextDocumentParser
from src.infrastructure.providers import DestinationPolicy, ProviderRegistry
from src.infrastructure.providers.builtins import BUILTIN_PROVIDERS
from src.infrastructure.repositories import mongo_base
from src.infrastructure.repositories.mongo_agent_config_repository import MongoAgentConfigRepository
from src.infrastructure.repositories.mongo_team_config_repository import MongoTeamConfigRepository
from src.infrastructure.web import app_factory
from src.infrastructure.web.app_factory import AppFactory
from src.presentation.controllers.orquestrador_controller import OrquestradorController
from tests.fakes import (
    FakeEmbedderFactory,
    FakeModelFactory,
    FakeMongoClient,
    FakeMongoCollection,
    InMemoryDocumentTreeRepository,
    InMemoryToolRepository,
    RecordingLogger,
)

pytestmark = pytest.mark.usefixtures("offline_knowledge")

CONN = "mongodb://mongo.invalid:27017"
SECRET_REF_VALUE = "sk-QA-F201-COLADO-NO-LUGAR-DA-REF"  # noqa: S105 - marcador de vazamento, não é segredo
URL_PASSWORD = "senha-QA-F201-NA-URL"  # noqa: S105 - marcador de vazamento, não é segredo
_ENV_NAMES = ("ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS", "APP_HOST", "API_KEY_RUN", "API_KEY_ADMIN")
LOCAL = {"base_url": "http://127.0.0.1:7777", "client": ("127.0.0.1", 50000)}

NEW_FIELDS = {
    "model_params": {"temperature": 0.2, "max_tokens": 256, "stream": False},
    "base_url": "https://gw.example.invalid:8443/v1",
    "api_key_ref": "env:QA_F201_API_KEY",
}


def _agent(agent_id: str, **extra: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "id": agent_id,
        "nome": f"Agente {agent_id}",
        "model": "llama3.2:latest",
        "descricao": "d",
        "prompt": "p",
        "tools_ids": [],
        "active": True,
    }
    doc.update(extra)
    return doc


def _team(team_id: str, members: list[str], **extra: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "id": team_id,
        "nome": f"Time {team_id}",
        "model": "qwen3",
        "prompt": "p",
        "member_ids": members,
        "mode": "route",
        "active": True,
    }
    doc.update(extra)
    return doc


def legacy_agent_docs() -> list[dict[str, Any]]:
    """Formas reais encontradas em produção: snake, camel, sem provider, rag com/sem modelo."""
    return [
        _agent("snake", factory_ia_model="openai", model="gpt-4o-mini"),
        _agent("camel", factoryIaModel="anthropic", model="claude-x", rag_config={"active": False}),
        _agent("sem-provider"),
        _agent("rag-padrao", rag_config={"active": True}),  # embedder default ollama/nomic
        _agent("rag-camel", rag_config={"active": True, "factoryIaModel": "gemini", "model": "gemini-embedding-001"}),
        _agent("rag-vazio", rag_config={"active": True, "model": ""}),  # semântico ignorado com aviso
        _agent("rag-hier", rag_config={"active": True, "search_strategy": "hierarchical", "doc_name": "x.md"}),
        _agent("ambos", factory_ia_model="groq", factoryIaModel="gemini"),
    ]


def legacy_team_docs() -> list[dict[str, Any]]:
    return [
        _team("t-canonico", ["snake", "camel"], factoryIaModel="ollama"),
        _team("t-snake", ["sem-provider"], factory_ia_model="openai", model="gpt-4o"),
        {k: v for k, v in _team("t-camel", []).items() if k != "member_ids"}
        | {"memberIds": ["rag-padrao"], "userMemoryActive": False, "factoryIaModel": "gemini"},
    ]


def with_new_fields(doc: dict[str, Any]) -> dict[str, Any]:
    out = dict(doc) | NEW_FIELDS
    rag = out.get("rag_config")
    explicit = isinstance(rag, dict) and rag.get("model") and (rag.get("factory_ia_model") or rag.get("factoryIaModel"))
    if explicit and rag.get("active"):  # campo novo no RAG exige model e provider explícitos
        out["rag_config"] = rag | {
            "model_params": {"dimensions": 8},
            "base_url": "http://emb.invalid:8080/v1",
            "api_key_ref": "env:EMB_API_KEY",
        }
    return out


class Boot:
    """Resultado de um startup real."""

    def __init__(self) -> None:
        self.agents: Any = None
        self.teams: Any = None
        self.config: Any = None
        self.status: tuple[int, int, int] = (0, 0, 0)
        self.logger = RecordingLogger()
        self.models = FakeModelFactory(responses=["oi"])
        self.embedders = FakeEmbedderFactory()
        self.registry: ProviderRegistry | None = None
        """Se definido, substitui as duas fábricas fake (modelo e embedder)."""

    def errors(self) -> list[tuple[str, dict[str, Any]]]:
        return [(r.message, r.context) for r in self.logger.records if r.level == "error"]

    def everything(self) -> str:
        return repr(self.logger.records) + json.dumps([self.agents, self.teams, self.config], default=str)


def boot(
    monkeypatch: pytest.MonkeyPatch,
    agent_docs: list[dict[str, Any]],
    team_docs: list[dict[str, Any]],
    registry: ProviderRegistry | None = None,
) -> Boot:
    result = Boot()
    result.registry = registry
    models: IModelFactory = registry or result.models
    embedders: IEmbedderFactory = registry or result.embedders
    client = FakeMongoClient(
        {"agents_config": FakeMongoCollection(agent_docs), "teams_config": FakeMongoCollection(team_docs)}
    )
    monkeypatch.setattr(mongo_base.MongoClientFactory, "get_client", classmethod(lambda cls, _conn: client))
    monkeypatch.setattr(agent_factory_service, "MongoAgentDb", lambda **_: None)
    monkeypatch.setattr(team_factory_service, "MongoAgentDb", lambda **_: None)
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    logger = result.logger
    agents_uc = GetActiveAgentsUseCase(
        AgentFactoryService(
            db_url=CONN,
            logger=logger,
            model_factory=models,
            embedder_factory=embedders,
            tool_factory=HttpToolFactory(logger=logger),
            tool_repository=InMemoryToolRepository(),
        ),
        MongoAgentConfigRepository(connection_string=CONN, logger=logger),
        logger,
    )
    teams_uc = GetActiveTeamsUseCase(
        TeamFactoryService(db_url=CONN, logger=logger, model_factory=models),
        MongoTeamConfigRepository(connection_string=CONN, logger=logger),
        logger,
    )
    controller = OrquestradorController(
        get_active_agents_use_case=agents_uc, get_active_teams_use_case=teams_uc, logger=logger
    )
    factory = AppFactory()
    app = factory.create_app()

    async def ensure_container() -> None:
        async def cleanup() -> None:
            return None

        factory._container = SimpleNamespace(  # type: ignore[assignment]
            config=factory._config,
            cleanup=cleanup,
            health_service=None,
            get_orquestrador_controller=lambda: controller,
        )

    monkeypatch.setattr(factory, "_ensure_container", ensure_container)
    monkeypatch.setattr(app_factory, "setup_telemetry", lambda config: None)
    monkeypatch.setattr(app_factory, "shutdown_telemetry", lambda: None)
    with TestClient(app, **LOCAL) as http:  # type: ignore[arg-type]
        agents, teams, config = http.get("/agents"), http.get("/teams"), http.get("/config")
        result.status = (agents.status_code, teams.status_code, config.status_code)
        result.agents, result.teams, result.config = agents.json(), teams.json(), config.json()
    return result


def _comparable(boot_result: Boot) -> dict[str, Any]:
    return {
        "status": boot_result.status,
        "agents": sorted(boot_result.agents, key=lambda a: a["id"]),
        "teams": sorted(boot_result.teams, key=lambda t: t["id"]),
        "config": {k: v for k, v in boot_result.config.items() if k != "os_id"},  # os_id é aleatório
        # provider/modelo pedidos; os campos novos (3º item) são conferidos à parte
        "model_calls": sorted(((p, m) for p, m, _ in boot_result.models.created), key=repr),
        "embedder_calls": sorted(((p, m) for p, m, _ in boot_result.embedders.created), key=repr),
        "log": sorted((m, repr(sorted(c.items()))) for m, c in boot_result.errors()),
    }


# ── (a) legado sobe igual, com e sem campos novos ───────────────────────────────────────────────


def test_startup_legado_expoe_todos_e_cria_modelos_com_provider_e_model_de_sempre(monkeypatch: pytest.MonkeyPatch):
    result = boot(monkeypatch, legacy_agent_docs(), legacy_team_docs())

    assert result.status == (200, 200, 200)
    assert sorted(a["id"] for a in result.agents) == sorted(d["id"] for d in legacy_agent_docs())
    assert sorted(t["id"] for t in result.teams) == ["t-camel", "t-canonico", "t-snake"]
    assert result.errors() == []
    # provider/model_id exatamente como o mapper antigo entregava; nenhum kwarg novo na criação
    assert sorted(result.models.created, key=repr) == sorted(
        [
            ("openai", "gpt-4o-mini", {}),
            ("anthropic", "claude-x", {}),
            ("ollama", "llama3.2:latest", {}),  # sem-provider
            ("ollama", "llama3.2:latest", {}),  # rag-padrao
            ("ollama", "llama3.2:latest", {}),  # rag-camel (o modelo do agente; o embedder é à parte)
            ("ollama", "llama3.2:latest", {}),  # rag-vazio
            ("ollama", "llama3.2:latest", {}),  # rag-hier
            ("groq", "llama3.2:latest", {}),  # ambos: snake vale
            ("ollama", "qwen3", {}),  # t-canonico
            ("openai", "gpt-4o", {}),  # t-snake
            ("gemini", "qwen3", {}),  # t-camel
        ],
        key=repr,
    )
    # RAG semântico: só os que tinham provider+modelo de texto não vazio
    assert sorted(result.embedders.created, key=repr) == sorted(
        [("ollama", "nomic-embed-text:latest", {}), ("gemini", "gemini-embedding-001", {})], key=repr
    )


def test_startup_com_campos_novos_validos_e_identico_ao_legado_e_os_campos_chegam_a_fabrica(
    monkeypatch: pytest.MonkeyPatch,
):
    legacy = boot(monkeypatch, legacy_agent_docs(), legacy_team_docs())
    new = boot(
        monkeypatch,
        [with_new_fields(d) for d in legacy_agent_docs()],
        [with_new_fields(d) for d in legacy_team_docs()],
    )

    assert _comparable(new) == _comparable(legacy)
    # F2-02: a criação usa os campos novos; todo modelo de agente/team os recebe
    assert [extras for _, _, extras in new.models.created] == [NEW_FIELDS] * len(new.models.created)
    assert sorted(new.embedders.created, key=repr) == sorted(
        [
            ("ollama", "nomic-embed-text:latest", {}),  # rag-padrao: sem provider/modelo explícitos
            (
                "gemini",
                "gemini-embedding-001",
                {"model_params": {"dimensions": 8}, "base_url": "http://emb.invalid:8080/v1",
                 "api_key_ref": "env:EMB_API_KEY"},
            ),
        ],
        key=repr,
    )


def test_campos_novos_nao_aparecem_em_resposta_http_nem_em_log(monkeypatch: pytest.MonkeyPatch):
    new = boot(
        monkeypatch, [with_new_fields(d) for d in legacy_agent_docs()], [with_new_fields(d) for d in legacy_team_docs()]
    )

    blob = new.everything()
    for forbidden in (
        "gw.example.invalid",
        "QA_F201_API_KEY",
        "EMB_API_KEY",
        "emb.invalid",
        "max_tokens",
        "api_key_ref",
        "base_url",
    ):
        assert forbidden not in blob, forbidden


# ── (b)/(c) campos novos hostis: isolam o documento e não vazam ─────────────────────────────────


HOSTILE_AGENT_FIELDS = [
    {"api_key_ref": SECRET_REF_VALUE},
    {"api_key_ref": f"env:{SECRET_REF_VALUE}"},
    {"api_key_ref": ""},
    {"api_key_ref": 7},
    {"base_url": f"http://admin:{URL_PASSWORD}@gw.example.invalid"},
    {"base_url": f"javascript:alert('{URL_PASSWORD}')"},
    {"base_url": f"file:///etc/{URL_PASSWORD}"},
    {"base_url": f"http://gw.example.invalid:99999/{URL_PASSWORD}"},
    {"base_url": f"http://gw.example.invalid/ {URL_PASSWORD}"},
    {"base_url": f"http://gw.example.invalid/?k={URL_PASSWORD}"},
    {"model_params": {"headers": {"Authorization": SECRET_REF_VALUE}}},
    {"model_params": [SECRET_REF_VALUE]},
    {"model_params": {"k": [SECRET_REF_VALUE]}},
    {"rag_config": {"active": True, "model": "e", "api_key_ref": SECRET_REF_VALUE}},
    {"rag_config": {"active": True, "model": None, "base_url": "http://emb.invalid"}},
]


@pytest.mark.parametrize(
    "hostile", HOSTILE_AGENT_FIELDS, ids=lambda h: next(iter(h)) + ":" + repr(next(iter(h.values())))[:30]
)
def test_agente_com_campo_novo_hostil_e_isolado_e_o_app_sobe_com_os_demais(
    monkeypatch: pytest.MonkeyPatch, hostile: dict[str, Any]
):
    docs = [_agent("antes"), _agent("ruim", **hostile), _agent("depois")]
    teams = [
        _team("t-ok", ["antes", "depois"]),
        _team("t-ruim", ["antes"], **{k: v for k, v in hostile.items() if k != "rag_config"}),
    ]

    result = boot(monkeypatch, docs, teams)

    assert result.status == (200, 200, 200)
    assert sorted(a["id"] for a in result.agents) == ["antes", "depois"]
    expect_team_bad = "rag_config" not in hostile
    assert sorted(t["id"] for t in result.teams) == (["t-ok"] if expect_team_bad else ["t-ok", "t-ruim"])
    ignored = [c for m, c in result.errors() if m.endswith("inválido ignorado")]
    assert {c.get("agent_id") for c in ignored if "agent_id" in c} == {"ruim"}
    assert all(c["error_type"] == "ValueError" for c in ignored)
    assert SECRET_REF_VALUE not in result.everything() and URL_PASSWORD not in result.everything()


def test_rag_com_tipo_hostil_no_modelo_nao_derruba_o_startup(monkeypatch: pytest.MonkeyPatch):
    """``model`` numérico/lista no rag_config (documento sem schema): nenhum agente cai por causa dele."""
    docs = [
        _agent("ok"),
        _agent("rag-int", rag_config={"active": True, "model": 5}),
        _agent("rag-lista", rag_config={"active": True, "model": ["x"], "factory_ia_model": ["y"]}),
        _agent("rag-obj", rag_config={"active": True, "model": {"$ne": 1}}),
    ]

    result = boot(monkeypatch, docs, [])

    assert result.status == (200, 200, 200)
    assert sorted(a["id"] for a in result.agents) == ["ok", "rag-int", "rag-lista", "rag-obj"]  # sobem, sem RAG
    assert result.embedders.created == [] and result.errors() == []
    warnings = [r.message for r in result.logger.records if r.level == "warning"]
    assert warnings.count("RAG ativo sem factory_ia_model ou model — ignorando") == 3
    assert SECRET_REF_VALUE not in result.everything()


def test_documento_invalido_nao_muda_a_ordem_nem_o_conjunto_dos_validos(monkeypatch: pytest.MonkeyPatch):
    valid = [_agent(f"v{i}", factoryIaModel="openai") for i in range(5)]
    mixed = [
        valid[0],
        _agent("x1", base_url="ftp://x"),
        valid[1],
        valid[2],
        _agent("x2", api_key_ref=SECRET_REF_VALUE),
        valid[3],
        valid[4],
    ]

    clean = boot(monkeypatch, valid, [])
    dirty = boot(monkeypatch, mixed, [])

    assert [a["id"] for a in dirty.agents] == [a["id"] for a in clean.agents]
    assert sorted(dirty.models.created, key=repr) == sorted(clean.models.created, key=repr)


# ── (d) indexação hierárquica: defaults de embedder preservados ─────────────────────────────────


class _Summary(ISummaryGenerator):
    async def generate_summary(self, content: str) -> str:
        return content[:20]


@pytest.mark.parametrize(
    ("rag", "expected"),
    [
        (RagConfig(active=True), ("ollama", "nomic-embed-text:latest")),
        (RagConfig(active=True, factory_ia_model="openai"), ("openai", "nomic-embed-text:latest")),
        (RagConfig(active=True, model="bge-m3"), ("ollama", "bge-m3")),
        (RagConfig(active=True, model="", factory_ia_model=""), ("ollama", "nomic-embed-text:latest")),
        (RagConfig(active=True, model="e", factory_ia_model="gemini"), ("gemini", "e")),
        (
            RagConfig(active=True, model="e", factory_ia_model="gemini", **NEW_FIELDS),  # type: ignore[arg-type]
            ("gemini", "e", NEW_FIELDS),  # F2-02: os campos novos chegam à fábrica
        ),
    ],
    ids=["sem-nada", "so-provider", "so-modelo", "vazios", "completo", "completo-com-campos-novos"],
)
async def test_indexacao_hierarquica_mantem_o_fallback_de_embedder(rag: RagConfig, expected: tuple[Any, ...]):
    embedders = FakeEmbedderFactory()
    service = DocumentIndexingService(
        parser=TextDocumentParser(),
        tree_repository=InMemoryDocumentTreeRepository(),
        summary_generator=_Summary(),
        embedder_factory=embedders,
        logger=RecordingLogger(),
    )
    rag.search_strategy = SearchStrategy.HIERARCHICAL

    nodes = await service.index_document("doc.md", "# Titulo\n\nTexto.\n\n## Secao\n\nOutro texto.\n", rag)

    assert nodes
    extras = expected[2] if len(expected) > 2 else {}
    assert embedders.created == [(expected[0], expected[1], extras)]


# ── F2-02: ProviderRegistry real recusa destino fora da allowlist ─────────────────────────────


def test_registry_real_isola_agente_com_base_url_fora_da_allowlist_sem_vazar(monkeypatch: pytest.MonkeyPatch):
    """Documento hostil (endpoint de coleta + chave por referência): o agente cai, os outros sobem,
    o motivo vai ao log sem o host nem a chave, e nada disso chega à resposta HTTP."""
    monkeypatch.setenv("QA_F202_API_KEY", SECRET_REF_VALUE)
    registry = ProviderRegistry(
        BUILTIN_PROVIDERS,
        policy=DestinationPolicy(allowlist=("gw.permitido.invalid",), resolver=lambda host: ["10.0.0.9"]),
    )
    compat = {"factory_ia_model": "openai_compatible", "api_key_ref": "env:QA_F202_API_KEY"}
    docs = [
        _agent("antes"),
        _agent("fora", base_url="https://coletor.evil.invalid/v1", **compat),
        _agent("metadata", base_url="http://169.254.169.254/latest", **compat),
        _agent("permitido", base_url="https://gw.permitido.invalid/v1", **compat),
    ]

    result = boot(monkeypatch, docs, [_team("t-fora", ["antes"], base_url="https://coletor.evil.invalid")], registry)

    assert result.status == (200, 200, 200)
    assert sorted(a["id"] for a in result.agents) == ["antes", "permitido"]
    assert result.teams == []
    failures = {c.get("agent_id") or c.get("team_id"): c for m, c in result.errors() if m.endswith("não carregado")}
    assert set(failures) == {"fora", "metadata", "t-fora"}
    assert all(c["error_type"] == "InvalidModelConfigError" for c in failures.values())
    assert "MODEL_BASE_URL_ALLOWLIST" in failures["fora"]["reason"]
    assert "metadata" in failures["metadata"]["reason"]
    blob = result.everything()
    for forbidden in (SECRET_REF_VALUE, "coletor.evil", "169.254.169.254", "QA_F202_API_KEY"):
        assert forbidden not in blob, forbidden
