"""QA F1-10: startup/refresh com documentos hostis no Mongo (coleção em memória).

Pilha: lifespan do ``AppFactory`` -> ``OrquestradorController`` -> use cases -> repositórios
Mongo reais sobre ``FakeMongoCollection`` -> factories reais (modelo fake) -> AgentOS.
Cobre o que ``test_startup_invalid_config_documents.py`` não cobre: tipos hostis em cada
campo, startup com todos os documentos inválidos, ``/admin/refresh-cache`` com documento
novo inválido, falha do banco no refresh e vazamento de conteúdo do documento em logs.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any

import pytest

from src.application.services import agent_factory_service, team_factory_service
from src.application.services.agent_factory_service import AgentFactoryService
from src.application.services.team_factory_service import TeamFactoryService
from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase
from src.application.use_cases.get_active_teams_use_case import GetActiveTeamsUseCase
from src.infrastructure.dependency_injection import HealthService
from src.infrastructure.http.http_tool_factory import HttpToolFactory
from src.infrastructure.logging.logger_adapter import StructlogLoggerAdapter
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
    InMemoryToolRepository,
    RecordingLogger,
    loopback_client,
)
from tests.fakes.logger import LoggedRecord

_ENV_NAMES = ("ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS", "APP_HOST", "API_KEY_RUN", "API_KEY_ADMIN")
CONN = "mongodb://mongo.invalid:27017"
SECRET = "sentinela-segredo-f110-9f3a1c"  # noqa: S105 - sentinela de teste, não é credencial


class TeeLogger(RecordingLogger):
    """Grava em memória e também no logger real (structlog), para checar a saída de verdade."""

    def __init__(self) -> None:
        super().__init__()
        self._real = StructlogLoggerAdapter("qa-f110")

    def _log(self, level: str, message: str, kwargs: dict[str, Any]) -> None:
        super()._log(level, message, kwargs)
        getattr(self._real, level)(message, **kwargs)


def _agent(agent_id: Any, **overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "id": agent_id,
        "nome": f"Agente {agent_id}",
        "factoryIaModel": "ollama",
        "model": "m",
        "descricao": "d",
        "prompt": "p",
        "tools_ids": [],
        "rag_config": {"active": False},
        "active": True,
    }
    doc.update(overrides)
    return doc


def _team(team_id: Any, members: list[str], **overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "id": team_id,
        "nome": f"Time {team_id}",
        "factoryIaModel": "ollama",
        "model": "m",
        "prompt": "p",
        "member_ids": members,
        "mode": "route",
        "active": True,
    }
    doc.update(overrides)
    return doc


class _Env:
    """Coleções em memória + controller real, reconstruídos a cada teste."""

    def __init__(self, monkeypatch: pytest.MonkeyPatch, agents: list[dict], teams: list[dict]) -> None:
        self.logger = TeeLogger()
        self.agents = FakeMongoCollection(agents)
        self.teams = FakeMongoCollection(teams)
        client = FakeMongoClient({"agents_config": self.agents, "teams_config": self.teams})
        monkeypatch.setattr(mongo_base.MongoClientFactory, "get_client", classmethod(lambda cls, _conn: client))
        monkeypatch.setattr(agent_factory_service, "MongoAgentDb", lambda **_: None)
        monkeypatch.setattr(team_factory_service, "MongoAgentDb", lambda **_: None)
        models = FakeModelFactory(responses=["oi"], invalid_models={"modelo-invalido"})
        logger = self.logger
        self.controller = OrquestradorController(
            get_active_agents_use_case=GetActiveAgentsUseCase(
                AgentFactoryService(
                    db_url=CONN,
                    logger=logger,
                    model_factory=models,
                    embedder_factory=FakeEmbedderFactory(),
                    tool_factory=HttpToolFactory(logger=logger),
                    tool_repository=InMemoryToolRepository(),
                ),
                MongoAgentConfigRepository(connection_string=CONN, logger=logger),
                logger,
            ),
            get_active_teams_use_case=GetActiveTeamsUseCase(
                TeamFactoryService(db_url=CONN, logger=logger, model_factory=models),
                MongoTeamConfigRepository(connection_string=CONN, logger=logger),
                logger,
            ),
            logger=logger,
        )

    def app(self, monkeypatch: pytest.MonkeyPatch) -> tuple[AppFactory, Any]:
        for name in _ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("AGNO_TELEMETRY", "false")
        factory = AppFactory()
        app = factory.create_app()
        controller = self.controller

        class _Mongo:
            class admin:
                @staticmethod
                async def command(_name: str) -> dict:
                    return {"ok": 1}

        async def ensure_container() -> None:
            async def cleanup() -> None:
                return None

            factory._container = SimpleNamespace(  # type: ignore[assignment]
                config=factory._config,
                cleanup=cleanup,
                health_service=HealthService(_Mongo(), self.logger),  # type: ignore[arg-type]
                get_orquestrador_controller=lambda: controller,
            )

        monkeypatch.setattr(factory, "_ensure_container", ensure_container)
        monkeypatch.setattr(app_factory, "setup_telemetry", lambda config: None)
        monkeypatch.setattr(app_factory, "shutdown_telemetry", lambda: None)
        return factory, app


def _ids(response: Any) -> list[str]:
    return sorted(item["id"] for item in response.json())


def _errors(logger: RecordingLogger) -> list[LoggedRecord]:
    return [r for r in logger.records if r.level == "error"]


# ── tipos hostis em cada campo: o app sobe e o agente válido segue de pé ─────

HOSTILE_AGENTS: dict[str, dict[str, Any]] = {
    "prompt-dict": _agent("h-prompt", prompt={"x": "y"}),
    "prompt-none": _agent("h-prompt-none", prompt=None),
    "tools-string": _agent("h-tools-str", tools_ids="tool-1"),
    "tools-dict": _agent("h-tools-dict", tools_ids={"a": 1}),
    "tools-lista-de-dict": _agent("h-tools-ld", tools_ids=[{"a": 1}]),
    "tools-none": _agent("h-tools-none", tools_ids=None),
    "memory-string": _agent("h-mem", user_memory_active="sim", summary_active={"x": 1}),
    "rag-lista": _agent("h-rag-lista", rag_config=["a"]),
    "rag-strategy-invalida": _agent("h-rag-strategy", rag_config={"active": True, "search_strategy": "xyz"}),
    "rag-doc-name-int": _agent("h-rag-doc", rag_config={"active": True, "doc_name": 12}),
    "rag-doc-name-traversal": _agent("h-rag-trav", rag_config={"active": True, "doc_name": "../../etc/passwd"}),
    "id-lista": _agent(["a", "b"]),
    "id-numero": _agent(12345),
    "id-com-barra": _agent("a/b"),
    "id-gigante": _agent("x" * 100_000),
    "id-nulo-unicode": _agent("a\x00b‮"),
    "nome-nulo": _agent("h-nome", nome=None),
    "model-lista": _agent("h-model", model=["m"]),
    "factory-numero": _agent("h-factory", factoryIaModel=3),
    "factory-desconhecida": _agent("h-factory-desc", factoryIaModel="nao-existe"),
    "active-string": _agent("h-active", active="true"),
}

HOSTILE_TEAMS: dict[str, dict[str, Any]] = {
    "membros-string": _team("t-membros-str", []) | {"member_ids": "valido-1"},
    "membros-dict": _team("t-membros-dict", []) | {"member_ids": {"a": 1}},
    "membros-lista-de-dict": _team("t-membros-ld", []) | {"member_ids": [{"a": 1}]},
    "membros-vazio": _team("t-membros-vazio", []),
    "membro-inexistente": _team("t-membro-inexistente", ["fantasma"]),
    "modo-lista": _team("t-modo-lista", ["valido-1"], mode=["route"]),
    "id-objeto": _team({"$ne": None}, ["valido-1"]),
    "prompt-dict": _team("t-prompt", ["valido-1"], prompt={"x": 1}),
    "user-memory-string": _team("t-um", ["valido-1"], user_memory_active="nao"),
    "id-igual-ao-do-agente": _team("valido-1", ["valido-1"]),
}


@pytest.mark.parametrize("name", sorted(HOSTILE_AGENTS))
def test_documento_de_agente_hostil_nao_derruba_o_startup_nem_o_agente_valido(
    monkeypatch: pytest.MonkeyPatch, name: str
):
    env = _Env(monkeypatch, [_agent("valido-1"), HOSTILE_AGENTS[name], _agent("valido-2")], [])
    _, app = env.app(monkeypatch)

    with loopback_client(app, raise_server_exceptions=False) as client:
        assert client.get("/livez").status_code == 200
        agents = client.get("/agents")
        run = client.post("/agents/valido-2/runs", data={"message": "oi", "stream": "false"})

    assert agents.status_code == 200, "AgentOS não montou: todos os agentes caíram junto com o documento hostil"
    assert {"valido-1", "valido-2"} <= set(_ids(agents))
    assert run.status_code == 200 and run.json()["content"] == "oi"


@pytest.mark.parametrize("name", sorted(HOSTILE_TEAMS))
def test_documento_de_team_hostil_nao_derruba_o_startup_nem_os_validos(monkeypatch: pytest.MonkeyPatch, name: str):
    env = _Env(
        monkeypatch,
        [_agent("valido-1"), _agent("valido-2")],
        [_team("time-ok", ["valido-1", "valido-2"]), HOSTILE_TEAMS[name]],
    )
    _, app = env.app(monkeypatch)

    with loopback_client(app, raise_server_exceptions=False) as client:
        assert client.get("/livez").status_code == 200
        agents = client.get("/agents")
        teams = client.get("/teams")

    assert agents.status_code == 200 and {"valido-1", "valido-2"} <= set(_ids(agents))
    assert teams.status_code == 200, "AgentOS não montou"
    assert "time-ok" in _ids(teams)


# ── todos os documentos inválidos ────────────────────────────────────


def test_startup_com_todos_os_documentos_invalidos_sobe_sem_agentes_e_responde_livez_e_health(
    monkeypatch: pytest.MonkeyPatch,
):
    env = _Env(
        monkeypatch,
        [_agent(["x"]), {"active": True}, _agent("sem-modelo", model=""), _agent("sem-nome", nome="")],
        [_team("t1", []), _team({"a": 1}, ["x"]), _team("t3", ["a"], mode="caos")],
    )
    _, app = env.app(monkeypatch)

    with loopback_client(app, raise_server_exceptions=False) as client:
        livez = client.get("/livez")
        health = client.get("/admin/health")
        stats = client.get("/metrics/cache")
        agents = client.get("/agents")
        ignored_at_startup = len([r for r in _errors(env.logger) if r.message.endswith("inválido ignorado")])
        refresh = client.post("/admin/refresh-cache")

    assert livez.status_code == 200 and livez.json() == {"status": "ok"}
    assert health.status_code == 200 and health.json()["status"] == "healthy"
    assert stats.json()["agents"]["agent_count"] == 0
    assert stats.json()["teams"]["team_count"] == 0
    assert agents.status_code == 404  # AgentOS não montado: só os endpoints admin
    assert refresh.status_code == 200
    assert ignored_at_startup == 7  # 4 agentes + 3 teams, cada um logado uma vez no startup
    assert len([r for r in _errors(env.logger) if r.message.endswith("inválido ignorado")]) == 14  # + o refresh


def test_colecoes_vazias_sobem_sem_agentes(monkeypatch: pytest.MonkeyPatch):
    env = _Env(monkeypatch, [], [])
    _, app = env.app(monkeypatch)

    with loopback_client(app, raise_server_exceptions=False) as client:
        assert client.get("/livez").status_code == 200
        assert client.get("/admin/health").status_code == 200

    assert _errors(env.logger) == []


# ── /admin/refresh-cache ─────────────────────────────────────────────


def test_refresh_cache_com_documento_novo_invalido_nao_derruba_o_app_nem_o_cache(monkeypatch: pytest.MonkeyPatch):
    env = _Env(monkeypatch, [_agent("valido-1"), _agent("valido-2")], [_team("time-ok", ["valido-1", "valido-2"])])
    _, app = env.app(monkeypatch)

    with loopback_client(app, raise_server_exceptions=False) as client:
        before = client.get("/metrics/cache").json()
        env.agents.docs.append(_agent({"$ne": None}))
        env.agents.docs.append({"active": True, "id": "sem-nome"})
        env.teams.docs.append(_team("time-ruim", []))
        refresh = client.post("/admin/refresh-cache")
        after = client.get("/metrics/cache").json()
        agents = client.get("/agents")
        run = client.post("/agents/valido-1/runs", data={"message": "oi", "stream": "false"})

    assert before["agents"]["agent_count"] == after["agents"]["agent_count"] == 2
    assert before["teams"]["team_count"] == after["teams"]["team_count"] == 1
    assert refresh.status_code == 200 and refresh.json() == {"status": "cache_refreshed"}
    assert _ids(agents) == ["valido-1", "valido-2"]
    assert run.status_code == 200
    # nenhum inválido no startup; o refresh ignora os 2 agentes e o team novos, com log de cada um
    ignored = [r for r in _errors(env.logger) if r.message.endswith("inválido ignorado")]
    assert len(ignored) == 3


def test_refresh_cache_nao_faz_hot_reload_de_rota_documentado_no_readme(monkeypatch: pytest.MonkeyPatch):
    """Agente novo e válido entra no cache, mas só aparece nas rotas após reiniciar (README)."""
    env = _Env(monkeypatch, [_agent("valido-1")], [])
    _, app = env.app(monkeypatch)

    with loopback_client(app, raise_server_exceptions=False) as client:
        env.agents.docs.append(_agent("novo"))
        refresh = client.post("/admin/refresh-cache")
        cache = client.get("/metrics/cache").json()
        agents = client.get("/agents")

    assert refresh.status_code == 200
    assert cache["agents"]["agent_count"] == 2
    assert _ids(agents) == ["valido-1"]


def test_refresh_cache_com_falha_do_banco_responde_erro_sem_texto_de_excecao(monkeypatch: pytest.MonkeyPatch):
    env = _Env(monkeypatch, [_agent("valido-1")], [])
    _, app = env.app(monkeypatch)

    def broken_find(query: dict) -> Any:
        raise RuntimeError(f"connection refused mongodb://user:{SECRET}@host")

    with loopback_client(app, raise_server_exceptions=False) as client:
        env.agents.find = broken_find  # type: ignore[method-assign]
        refresh = client.post("/admin/refresh-cache")
        livez = client.get("/livez")
        agents = client.get("/agents")

    assert livez.status_code == 200
    assert agents.status_code == 200 and _ids(agents) == ["valido-1"]  # AgentOS do startup segue
    assert refresh.status_code == 500
    assert SECRET not in refresh.text


# ── formato legado de team ───────────────────────────────────────────


def test_team_legado_camelcase_completo_carrega_e_snake_case_vence_quando_ha_os_dois(
    monkeypatch: pytest.MonkeyPatch,
):
    legado = {
        "id": "legado",
        "nome": "Legado",
        "model": "m",
        "factoryIaModel": "ollama",
        "mode": "coordinate",
        "prompt": "p",
        "memberIds": ["valido-1"],
        "userMemoryActive": False,
        "summaryActive": True,
        "active": True,
    }
    misto = copy.deepcopy(legado) | {
        "id": "misto",
        "member_ids": ["valido-2"],
        "user_memory_active": True,
        "summary_active": False,
    }
    env = _Env(monkeypatch, [_agent("valido-1"), _agent("valido-2")], [legado, misto])
    _, app = env.app(monkeypatch)

    with loopback_client(app, raise_server_exceptions=False) as client:
        teams = client.get("/teams")
        detail = {tid: client.get(f"/teams/{tid}").json() for tid in ("legado", "misto")}

    assert _ids(teams) == ["legado", "misto"]
    assert [m["id"] for m in detail["legado"]["members"]] == ["valido-1"]
    assert [m["id"] for m in detail["misto"]["members"]] == ["valido-2"]
    assert _errors(env.logger) == []


# ── logs não carregam conteúdo do documento ──────────────────────────


def test_logs_de_documento_invalido_nao_trazem_conteudo_do_documento(
    monkeypatch: pytest.MonkeyPatch, capfd: pytest.CaptureFixture[str], caplog: pytest.LogCaptureFixture
):
    caplog.set_level("DEBUG")
    hostile_agents = [
        _agent("a-estrategia", prompt=SECRET, descricao=SECRET, rag_config={"active": True, "search_strategy": SECRET}),
        _agent("a-factory", prompt=SECRET, factoryIaModel=SECRET),  # factory desconhecida: falha na criação
        _agent("a-modelo", prompt=SECRET, model="modelo-invalido"),
        _agent({"k": SECRET}, prompt=SECRET),
        _agent(SECRET, nome=None, prompt=SECRET),  # id é texto: pode ser logado, mas o segredo é o prompt
        {"id": "a-sem-modelo", "nome": SECRET, "model": "", "prompt": SECRET, "active": True},
    ]
    hostile_teams = [
        _team("t-modo", ["valido-1"], mode=SECRET, prompt=SECRET),
        _team({"k": SECRET}, ["valido-1"], prompt=SECRET),
        _team("t-vazio", [], prompt=SECRET, descricao=SECRET),
        _team("t-fantasma", ["membro-fantasma"], prompt=SECRET),
    ]
    env = _Env(monkeypatch, [_agent("valido-1"), *hostile_agents], [_team("time-ok", ["valido-1"]), *hostile_teams])
    _, app = env.app(monkeypatch)

    with loopback_client(app, raise_server_exceptions=False) as client:
        client.get("/agents")
        client.post("/admin/refresh-cache")

    out = capfd.readouterr()
    records = [repr(r) for r in env.logger.records if r.context.get("agent_id") != SECRET]
    assert not any(SECRET in r for r in records), [r for r in records if SECRET in r]
    leaked = [line for line in (out.out + out.err + caplog.text).splitlines() if SECRET in line]
    # o único lugar onde o texto pode aparecer é o id textual do agente (campo ``agent_id``)
    assert all("agent_id" in line or "a-sem-modelo" in line for line in leaked), leaked
    assert env.logger.messages("error")  # houve erros logados: o teste não é vácuo


# ── bugs de produção encontrados pelo QA (corrigidos na rodada 2) ────


def test_documento_de_agente_com_id_duplicado_nao_derruba_os_outros(monkeypatch: pytest.MonkeyPatch):
    env = _Env(monkeypatch, [_agent("valido-1"), _agent("valido-1", nome="Repetido"), _agent("valido-2")], [])
    _, app = env.app(monkeypatch)

    with loopback_client(app, raise_server_exceptions=False) as client:
        agents = client.get("/agents")

    assert agents.status_code == 200
    assert {"valido-1", "valido-2"} <= set(_ids(agents))


@pytest.mark.parametrize("value", [7, {"a": 1}, ["x"]])
def test_agente_com_descricao_nao_texto_nao_quebra_a_listagem_dos_outros(monkeypatch: pytest.MonkeyPatch, value: Any):
    env = _Env(monkeypatch, [_agent("valido-1"), _agent("h-descricao", descricao=value)], [])
    _, app = env.app(monkeypatch)

    with loopback_client(app, raise_server_exceptions=False) as client:
        agents = client.get("/agents")
        run = client.post("/agents/valido-1/runs", data={"message": "oi", "stream": "false"})

    assert run.status_code == 200  # a execução segue funcionando
    assert agents.status_code == 200
    assert "valido-1" in _ids(agents)
