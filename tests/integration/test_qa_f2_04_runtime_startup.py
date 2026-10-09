"""QA F2-04: startup real pelo ``AgnoRuntime``: o refactor não mudou o comportamento observável.

Pilha real: ``AppFactory`` -> ``DependencyContainer`` (registry de providers real, trocado só no
``create_model``/``create_embedder`` por modelo/embedder falsos) -> ``AgnoRuntime`` -> use cases ->
repositórios Mongo sobre coleção fake -> AgentOS. Agentes: simples, memória de usuário, tool HTTP
(com tool ausente), RAG semântico, RAG hierárquico (tool chamada pelo modelo roteirizado),
``doc_name`` hostil, modelo recusado, SDK que falha, id repetido, documento inválido. Teams:
membros válidos/ausentes, id repetido, modelo recusado, SDK que falha, modo ``coordinate``.

As expectativas literais foram obtidas rodando este mesmo cenário (script diferencial) no commit
base ``d8bdd04`` e na árvore nova: a saída (ids e ordem das listas, detalhe de cada entidade,
sequência de erros logados, resposta de cada run com e sem ``user_id``, mensagens que o modelo
recebe, estatísticas de cache) foi idêntica. Diferenças, ambas na falha da busca da tool
hierárquica (``test_falha_da_busca_hierarquica...``): o ``warning`` novo (só o tipo do erro) e o
resultado da tool ao modelo, que deixou de ecoar a exceção e virou mensagem fixa (revisão R1).
"""

from __future__ import annotations

import copy
from collections import defaultdict
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from agno.agent import Agent
from agno.db.in_memory import InMemoryDb
from agno.models.response import ModelResponse
from agno.team import Team
from starlette.testclient import TestClient

from src.domain.ports import InvalidModelConfigError
from src.infrastructure import dependency_injection as di
from src.infrastructure.repositories import mongo_base
from src.infrastructure.runtime.agno import agent_factory_service, team_factory_service
from src.infrastructure.runtime.agno import runtime as agno_runtime_module
from src.infrastructure.web import app_factory
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import (
    FakeChatModel,
    FakeEmbedder,
    FakeMongoClient,
    FakeMongoCollection,
    InMemoryDocumentTreeRepository,
    RecordingLogger,
    running_on_event_loop,
)
from tests.fakes.knowledge import cut_agno_io

LOCAL = {"base_url": "http://127.0.0.1:7777", "client": ("127.0.0.1", 50000)}
SECRET = "SEGREDO-QA-F204-DA-EXCECAO"  # noqa: S105 - marcador de vazamento, não é credencial
_ENV_CLEAR = (
    "API_KEY_RUN", "API_KEY_ADMIN", "APP_HOST", "ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS",
    "PLUGIN_ALLOWLIST", "ALLOW_DYNAMIC_IMPORT", "DYNAMIC_PROVIDER_SPECS", "MODEL_BASE_URL_ALLOWLIST", "SECRETS_DIR",
)

HIER_CALL = ModelResponse(
    role="assistant",
    tool_calls=[
        {
            "id": "c1",
            "type": "function",
            "function": {"name": "search_knowledge", "arguments": '{"query": "reembolso"}'},
        }
    ],
)


class _Collection(FakeMongoCollection):
    """Coleção fake com ``create_index`` e o ``$in`` por ``id`` que o repositório de tools usa."""

    async def create_index(self, *_: object, **__: object) -> str:
        return "idx"

    def find(self, query: dict[str, Any]) -> Any:
        wanted = query.get("id")
        if isinstance(wanted, dict) and "$in" in wanted:
            rest = {k: v for k, v in query.items() if k != "id"}
            return FakeMongoCollection([d for d in self.docs if d.get("id") in wanted["$in"]]).find(rest)
        return super().find(query)


class _Tree(InMemoryDocumentTreeRepository):
    async def ensure_indexes(self) -> None:
        return None


class _Motor:
    def __init__(self, *_: object, **__: object) -> None:
        self.admin = self

    async def command(self, *_: object) -> dict[str, int]:
        return {"ok": 1}

    def close(self) -> None:
        return None


def _agent(agent_id: Any, **extra: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "id": agent_id, "nome": f"Agente {agent_id}", "factoryIaModel": "ollama", "model": f"m-{agent_id}",
        "descricao": "desc", "prompt": "prompt", "tools_ids": [], "rag_config": {"active": False}, "active": True,
    }
    doc.update(extra)
    return doc


def _team(team_id: Any, members: list[str], **extra: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "id": team_id, "nome": f"Time {team_id}", "factoryIaModel": "ollama", "model": f"mt-{team_id}",
        "prompt": "p", "member_ids": members, "mode": "route", "active": True,
    }
    doc.update(extra)
    return doc


TOOL_DOC = {
    "id": "cep", "name": "cep", "description": "Consulta CEP", "route": "http://upstream.invalid/cep/{cep}",
    "http_method": "GET", "parameters": [{"name": "cep", "type": "string", "description": "d", "required": True}],
    "instructions": "so digitos", "headers": {}, "active": True,
}


def _rag(strategy: str, doc: str) -> dict[str, Any]:
    return {"active": True, "doc_name": doc, "factory_ia_model": "ollama", "model": "emb", "search_strategy": strategy}


AGENT_DOCS = [
    _agent("a-simples"),
    _agent("a-mem", user_memory_active=True),
    _agent("a-tool", tools_ids=["cep", "fantasma"]),
    _agent("a-sem", rag_config=_rag("semantic", "fin.md")),
    _agent("a-hier", model="m-hier", rag_config=_rag("hierarchical", "hier.md")),
    _agent("a-hier-ruim", rag_config={"active": True, "doc_name": "../etc/passwd", "search_strategy": "hierarchical"}),
    _agent("a-recusado", model="modelo-invalido"),
    _agent("a-boom", model="modelo-boom"),
    _agent("a-simples", nome="Repetido"),
    _agent({"k": "v"}),
    _agent("a-ultimo"),
]
TEAM_DOCS = [
    _team("t-ok", ["a-simples", "a-mem"]),
    _team("t-mem", ["a-simples"], user_memory_active=True),
    _team("t-sem-mem", ["a-simples"], user_memory_active=False),
    _team("t-fantasma", ["fantasma"]),
    _team("t-parcial", ["a-simples", "fantasma"]),
    _team("t-ok", ["a-ultimo"], nome="Repetido"),
    _team("t-recusado", ["a-simples"], model="modelo-invalido"),
    _team("t-boom", ["a-simples"], model="modelo-boom"),
    _team("t-coordinate", ["a-tool", "a-ultimo"], mode="coordinate"),
]
LOADED_AGENTS = ["a-simples", "a-mem", "a-tool", "a-sem", "a-hier", "a-hier-ruim", "a-ultimo"]
LOADED_TEAMS = ["t-ok", "t-mem", "t-sem-mem", "t-parcial", "t-coordinate"]


@dataclass
class Booted:
    logger: RecordingLogger = field(default_factory=RecordingLogger)
    models: dict[str, list[FakeChatModel]] = field(default_factory=dict)
    created_on_loop: dict[str, bool] = field(default_factory=dict)
    agents: list[dict[str, Any]] = field(default_factory=list)
    teams: list[dict[str, Any]] = field(default_factory=list)
    agent_details: dict[str, dict[str, Any]] = field(default_factory=dict)
    team_details: dict[str, dict[str, Any]] = field(default_factory=dict)
    runs: dict[str, tuple[int, dict[str, Any]]] = field(default_factory=dict)
    cache_boot: dict[str, Any] = field(default_factory=dict)
    cache_after: dict[str, Any] = field(default_factory=dict)
    refresh: tuple[int, dict[str, Any]] = (0, {})
    handles: tuple[list[Any], list[Any]] = field(default_factory=lambda: ([], []))

    def errors(self) -> list[tuple[str, dict[str, Any]]]:
        return [(r.message, r.context) for r in self.logger.records if r.level == "error"]

    def calls(self, model_id: str) -> int:
        return sum(len(m.calls) for m in self.models.get(model_id, []))


def _boot(root: Path, *, search_fails: bool) -> Booted:
    result = Booted()
    with pytest.MonkeyPatch.context() as mp:
        (root / "docs").mkdir()
        (root / "docs" / "fin.md").write_text("# Fin\n\nreembolso e fatura.\n", encoding="utf-8")
        (root / "docs" / "hier.md").write_text(
            "# Fin\n\nIntro.\n\n## Reembolso\n\nenvie a nota.\n\n## Fatura\n\nvence dia 5.\n", encoding="utf-8"
        )
        mp.chdir(root)
        for name in _ENV_CLEAR:
            mp.delenv(name, raising=False)
        mp.setenv("AGNO_TELEMETRY", "false")
        mp.setenv("OTEL_ENABLED", "false")
        collections: defaultdict[str, FakeMongoCollection] = defaultdict(_Collection)
        collections["agents_config"] = _Collection(copy.deepcopy(AGENT_DOCS))
        collections["teams_config"] = _Collection(copy.deepcopy(TEAM_DOCS))
        collections["tools"] = _Collection([copy.deepcopy(TOOL_DOC)])
        client = FakeMongoClient(collections)
        memory_db = InMemoryDb()
        mp.setattr(mongo_base.MongoClientFactory, "get_client", classmethod(lambda cls, _conn: client))
        mp.setattr(di, "AsyncIOMotorClient", _Motor)
        mp.setattr(di, "StructlogLoggerAdapter", lambda _name: result.logger)
        mp.setattr(di, "MongoDocumentTreeRepository", lambda **_: _Tree())
        mp.setattr(agent_factory_service, "MongoAgentDb", lambda **_: memory_db)
        mp.setattr(team_factory_service, "MongoAgentDb", lambda **_: memory_db)
        mp.setattr(app_factory, "setup_telemetry", lambda config: None)
        mp.setattr(app_factory, "shutdown_telemetry", lambda: None)

        def create_model(self: Any, config: Any) -> FakeChatModel:
            result.created_on_loop[config.model_id] = running_on_event_loop()
            if config.model_id == "modelo-invalido":
                raise InvalidModelConfigError("modelo recusado pela validação (texto nosso)")
            if config.model_id == "modelo-boom":
                raise RuntimeError(SECRET)
            responses: list[str | ModelResponse] = ["resposta-A", "resposta-B"]
            if config.model_id == "m-hier":
                responses = [HIER_CALL, "final-hier"]
            model = FakeChatModel(id=config.model_id, provider=config.provider, responses=responses)
            result.models.setdefault(config.model_id, []).append(model)
            return model

        def create_embedder(self: Any, config: Any) -> FakeEmbedder:
            return FakeEmbedder(id=config.model_id, provider=config.provider)

        mp.setattr(di.ProviderRegistry, "create_model", create_model)
        mp.setattr(di.ProviderRegistry, "create_embedder", create_embedder)
        if search_fails:
            from src.application.services.search_strategies.hierarchical_search_strategy import (
                HierarchicalSearchStrategy,
            )

            async def failing(self: Any, *args: Any, **kwargs: Any) -> Any:
                raise RuntimeError(SECRET)

            mp.setattr(HierarchicalSearchStrategy, "search", failing)

        real_agent_os = agno_runtime_module.AgentOS

        class SpyAgentOS(real_agent_os):  # type: ignore[valid-type, misc]
            """Registra o que o AgentOS recebe do runtime."""

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                result.handles = (list(kwargs.get("agents") or []), list(kwargs.get("teams") or []))
                super().__init__(*args, **kwargs)

        mp.setattr(agno_runtime_module, "AgentOS", SpyAgentOS)

        with cut_agno_io(mp, on_insert=lambda *_: None):
            app = AppFactory().create_app()
            with TestClient(app, **LOCAL) as http:  # type: ignore[arg-type]
                result.agents = http.get("/agents").json()
                result.teams = http.get("/teams").json()
                result.agent_details = {a["id"]: http.get(f"/agents/{a['id']}").json() for a in result.agents}
                result.team_details = {t["id"]: http.get(f"/teams/{t['id']}").json() for t in result.teams}
                result.cache_boot = http.get("/metrics/cache").json()
                for kind, entities in (("agents", result.agents), ("teams", result.teams)):
                    for entity in entities:
                        for user in (None, "ana"):
                            data = {"message": "oi", "stream": "false"} | ({"user_id": user} if user else {})
                            response = http.post(f"/{kind}/{entity['id']}/runs", data=data)
                            result.runs[f"{kind}:{entity['id']}:{user}"] = (response.status_code, response.json())
                response = http.post("/admin/refresh-cache")
                result.refresh = (response.status_code, response.json())
                result.cache_after = http.get("/metrics/cache").json()
    return result


@pytest.fixture(scope="module")
def boot(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Booted]:
    yield _boot(tmp_path_factory.mktemp("f204_ok"), search_fails=False)


@pytest.fixture(scope="module")
def boot_search_fails(tmp_path_factory: pytest.TempPathFactory) -> Iterator[Booted]:
    yield _boot(tmp_path_factory.mktemp("f204_fail"), search_fails=True)


# ── ids, ordem e erros: iguais aos do commit base ───────────────────────────────────────────────


def test_agentes_e_teams_expostos_tem_os_mesmos_ids_e_ordem(boot: Booted) -> None:
    assert [a["id"] for a in boot.agents] == LOADED_AGENTS
    assert [t["id"] for t in boot.teams] == LOADED_TEAMS


def test_id_repetido_vence_o_primeiro_em_agente_e_em_team(boot: Booted) -> None:
    assert boot.agent_details["a-simples"]["name"] == "Agente a-simples"
    assert boot.team_details["t-ok"]["name"] == "Time t-ok"
    assert [m["id"] for m in boot.team_details["t-ok"]["members"]] == ["a-simples", "a-mem"]


def test_sequencia_de_erros_logados_no_startup_e_a_mesma_do_commit_base(boot: Booted) -> None:
    startup = boot.errors()[: len(boot.errors()) // 2]  # o refresh repete a mesma carga

    assert startup == [
        ("Documento de agente inválido ignorado", {"agent_id": None, "error_type": "ValueError", "mongo_id": None}),
        ("Agente com id repetido ignorado", {"agent_id": "a-simples"}),
        (
            "Tool referenciada pelo agente não encontrada (ausente, inativa ou inválida); ignorada",
            {"agent_id": "a-tool", "tool_id": "fantasma"},
        ),
        ("doc_name do RAG rejeitado; RAG do agente desativado", startup[3][1]),
        (
            "Agente não carregado",
            {"agent_id": "a-recusado", "error_type": "InvalidModelConfigError",
             "reason": "modelo recusado pela validação (texto nosso)"},
        ),
        ("Agente não carregado", {"agent_id": "a-boom", "error_type": "RuntimeError"}),
        ("Team com id repetido ignorado", {"team_id": "t-ok"}),
        ("Team não carregado", {"team_id": "t-fantasma", "error_type": "ValueError"}),
        (
            "Team não carregado",
            {"team_id": "t-recusado", "error_type": "InvalidModelConfigError",
             "reason": "modelo recusado pela validação (texto nosso)"},
        ),
        ("Team não carregado", {"team_id": "t-boom", "error_type": "RuntimeError"}),
    ]
    assert startup[3][1]["agent_id"] == "a-hier-ruim" and "'..'" in startup[3][1]["reason"]
    assert boot.errors() == startup + startup


def test_texto_da_excecao_de_sdk_nunca_chega_ao_log(boot: Booted) -> None:
    assert SECRET not in repr(boot.logger.records)


def test_modelos_de_agentes_e_teams_sao_criados_fora_do_event_loop(boot: Booted) -> None:
    assert boot.created_on_loop  # não vazio
    assert {k: v for k, v in boot.created_on_loop.items() if v} == {}
    assert {"m-a-simples", "mt-t-ok", "mt-t-coordinate", "modelo-boom"} <= set(boot.created_on_loop)


# ── membros e entidades ─────────────────────────────────────────────────────────────────────────


def test_membros_dos_teams_so_os_agentes_que_existem(boot: Booted) -> None:
    assert {tid: [m["id"] for m in d["members"]] for tid, d in boot.team_details.items()} == {
        "t-ok": ["a-simples", "a-mem"],
        "t-mem": ["a-simples"],
        "t-sem-mem": ["a-simples"],
        "t-parcial": ["a-simples"],
        "t-coordinate": ["a-tool", "a-ultimo"],
    }
    assert boot.team_details["t-coordinate"]["mode"] == "coordinate"


def test_agente_com_tool_expoe_a_tool_http_e_ignora_a_ausente(boot: Booted) -> None:
    tools = boot.agent_details["a-tool"]["tools"]["tools"]

    assert [t["name"] for t in tools] == ["cep"]


def test_agente_com_rag_semantico_e_com_memoria_expoem_as_tools_do_framework(boot: Booted) -> None:
    sem = [t["name"] for t in boot.agent_details["a-sem"]["tools"]["tools"]]
    mem = boot.agent_details["a-mem"]

    assert "search_knowledge_base" in sem
    assert mem["memory"]["enable_user_memories"] is True and mem["memory"]["enable_agentic_memory"] is True
    assert boot.agent_details["a-simples"].get("knowledge") is None


def test_agentos_recebeu_os_handles_como_objetos_do_agno(boot: Booted) -> None:
    agents, teams = boot.handles

    assert all(isinstance(a, Agent) for a in agents) and all(isinstance(t, Team) for t in teams)
    assert [a.id for a in agents] == LOADED_AGENTS
    assert [t.id for t in teams] == LOADED_TEAMS
    assert all(isinstance(m, Agent) and m in agents for t in teams for m in t.members)  # membros = handles do cache


# ── guardrail de user_id aplicado pelo runtime novo ─────────────────────────────────────────────


@pytest.mark.parametrize("agent_id", ["a-mem"])
def test_agente_com_memoria_recusa_run_sem_user_id_e_nao_chama_o_modelo(boot: Booted, agent_id: str) -> None:
    status, body = boot.runs[f"agents:{agent_id}:None"]

    assert status == 200 and body["content"].startswith(f"user_id obrigatório: '{agent_id}' guarda memória")
    assert boot.calls("m-a-mem") == 1  # só o run com user_id chamou o modelo
    assert boot.runs[f"agents:{agent_id}:ana"][1]["content"] == "resposta-A"


@pytest.mark.parametrize("team_id", ["t-ok", "t-mem", "t-parcial", "t-coordinate"])
def test_team_com_memoria_recusa_run_sem_user_id(boot: Booted, team_id: str) -> None:
    refused = boot.runs[f"teams:{team_id}:None"][1]["content"]
    accepted = boot.runs[f"teams:{team_id}:ana"][1]["content"]

    assert refused.startswith(f"user_id obrigatório: '{team_id}' guarda memória")
    assert accepted == "resposta-A"


def test_team_sem_memoria_e_agentes_sem_memoria_aceitam_run_sem_user_id(boot: Booted) -> None:
    assert boot.runs["teams:t-sem-mem:None"][1]["content"] == "resposta-A"
    for agent_id in ("a-simples", "a-tool", "a-sem", "a-ultimo", "a-hier-ruim"):
        assert boot.runs[f"agents:{agent_id}:None"][1]["content"] == "resposta-A", agent_id


# ── RAG hierárquico: tool do agno chamada pelo modelo, resultado volta a ele ───────────────────


def test_tool_hierarquica_e_chamada_pelo_modelo_e_o_resultado_volta_a_ele(boot: Booted) -> None:
    first = boot.models["m-hier"][0]

    assert [c.tool_names for c in first.calls[:2]] == [("search_knowledge",), ("search_knowledge",)]
    tool_messages = [content for role, content in first.calls[1].messages if role == "tool"]
    assert tool_messages == ["Nenhuma informação relevante encontrada no knowledge base."]
    assert boot.runs["agents:a-hier:None"][1]["content"] == "final-hier"


def test_falha_da_busca_hierarquica_vai_ao_log_so_com_o_tipo_e_ao_modelo_so_a_mensagem_fixa(
    boot_search_fails: Booted,
) -> None:
    """Mudanças de comportamento do F2-04: ``warning`` novo e mensagem fixa ao modelo (sem texto da exceção)."""
    first = boot_search_fails.models["m-hier"][0]
    tool_messages = [content for role, content in first.calls[1].messages if role == "tool"]
    warnings = [r for r in boot_search_fails.logger.records if r.message.startswith("Erro na busca hierárquica")]

    assert tool_messages == ["Erro ao buscar no knowledge base; a busca não está disponível agora."]
    assert all(SECRET not in str(message) for call in first.calls for message in call.messages)
    assert [(w.level, w.context) for w in warnings] == [("warning", {"error_type": "RuntimeError"})]
    assert SECRET not in repr(boot_search_fails.logger.records)


def test_doc_name_hostil_desativa_o_rag_so_do_agente_dele(boot: Booted) -> None:
    assert "a-hier-ruim" in [a["id"] for a in boot.agents]
    assert boot.runs["agents:a-hier-ruim:None"][1]["content"] == "resposta-A"
    assert boot.calls("m-a-hier-ruim") == 2  # sem tool de busca: o modelo não recebeu tool


# ── cache do controller com os handles ──────────────────────────────────────────────────────────


def test_cache_do_controller_conta_entidades_e_o_refresh_recarrega_sem_perder_nada(boot: Booted) -> None:
    assert boot.cache_boot["agents"]["agent_count"] == len(LOADED_AGENTS)
    assert boot.cache_boot["teams"]["team_count"] == len(LOADED_TEAMS)
    assert boot.refresh == (200, {"status": "cache_refreshed"})
    assert boot.cache_after["agents"]["agent_count"] == len(LOADED_AGENTS)
    assert boot.cache_after["teams"]["team_count"] == len(LOADED_TEAMS)
    assert boot.cache_after["agents"]["hit_count"] == 0  # cache novo depois do refresh
