"""F1-10 (B7): startup real com documentos inválidos em ``agents_config``/``teams_config``.

Pilha: lifespan do ``AppFactory`` -> ``OrquestradorController`` -> use cases ->
``MongoAgentConfigRepository``/``MongoTeamConfigRepository`` reais sobre a coleção em memória
-> ``AgentFactoryService``/``TeamFactoryService`` reais (modelo fake, sem Mongo do agno) ->
AgentOS montado. Documento inválido e agente/team que falha na criação viram log de erro com
id; o app sobe e expõe os válidos.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient

from src.application.services import agent_factory_service, team_factory_service
from src.application.services.agent_factory_service import AgentFactoryService
from src.application.services.team_factory_service import TeamFactoryService
from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase
from src.application.use_cases.get_active_teams_use_case import GetActiveTeamsUseCase
from src.infrastructure.http.http_tool_factory import HttpToolFactory
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
)

_ENV_NAMES = ("ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS", "APP_HOST", "API_KEY_RUN", "API_KEY_ADMIN")
CONN = "mongodb://mongo.invalid:27017"
MARKER = "SEGREDO-DO-DOCUMENTO"


def _agent(agent_id: Any, **overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "id": agent_id,
        "nome": f"Agente {agent_id}",
        "factoryIaModel": "ollama",
        "model": "m",
        "descricao": "d",
        "prompt": [MARKER],
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
        "prompt": MARKER,
        "member_ids": members,
        "mode": "route",
        "active": True,
    }
    doc.update(overrides)
    return doc


AGENT_DOCS = [
    _agent("valido-1"),
    {k: v for k, v in _agent("x").items() if k != "id"},  # sem id
    _agent({"$ne": None}),  # id objeto: antes derrubava a montagem do AgentOS
    _agent("rag-quebrado", rag_config="ligado"),
    _agent("modelo-recusado", model="modelo-invalido"),  # falha na criação (factory)
    _agent("valido-2"),
    _agent("inativo", active=False),
    _agent("valido-1", nome="Repetido"),  # id repetido: antes o AgentOS recusava a montagem inteira
    _agent("descricao-objeto", descricao={"pt": "x"}),  # antes: 500 em /agents e /config para todos
]
TEAM_DOCS = [
    _team("time-ok", ["valido-1", "valido-2"]),
    _team("time-modo", ["valido-1"], mode="caos"),
    _team(["lista"], ["valido-1"]),
    _team("time-sem-membro-ativo", ["modelo-recusado"]),  # falha na criação (sem membro)
    {k: v for k, v in _team("time-legado", []).items() if k != "member_ids"}
    | {"memberIds": ["valido-2"], "userMemoryActive": False},
    _team("time-ok", ["valido-2"], nome="Repetido"),
    _team("time-descricao", ["valido-1"], descricao=7),
]


def _controller(monkeypatch: pytest.MonkeyPatch, logger: RecordingLogger) -> OrquestradorController:
    client = FakeMongoClient(
        {"agents_config": FakeMongoCollection(AGENT_DOCS), "teams_config": FakeMongoCollection(TEAM_DOCS)}
    )
    monkeypatch.setattr(mongo_base.MongoClientFactory, "get_client", classmethod(lambda cls, _conn: client))
    monkeypatch.setattr(agent_factory_service, "MongoAgentDb", lambda **_: None)
    monkeypatch.setattr(team_factory_service, "MongoAgentDb", lambda **_: None)
    models = FakeModelFactory(responses=["oi"], invalid_models={"modelo-invalido"})
    agents = GetActiveAgentsUseCase(
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
    )
    teams = GetActiveTeamsUseCase(
        TeamFactoryService(db_url=CONN, logger=logger, model_factory=models),
        MongoTeamConfigRepository(connection_string=CONN, logger=logger),
        logger,
    )
    return OrquestradorController(get_active_agents_use_case=agents, get_active_teams_use_case=teams, logger=logger)


def _start(monkeypatch: pytest.MonkeyPatch, logger: RecordingLogger) -> tuple[AppFactory, Any]:
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    factory = AppFactory()
    app = factory.create_app()
    controller = _controller(monkeypatch, logger)

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
    return factory, app


def test_startup_com_documentos_invalidos_sobe_e_expoe_so_os_validos(monkeypatch: pytest.MonkeyPatch):
    logger = RecordingLogger()
    _, app = _start(monkeypatch, logger)
    local = {"base_url": "http://127.0.0.1:7777", "client": ("127.0.0.1", 50000)}

    with TestClient(app, **local) as client:  # type: ignore[arg-type]
        agents = client.get("/agents")
        teams = client.get("/teams")
        config = client.get("/config")
        run = client.post("/agents/valido-2/runs", data={"message": "oi", "stream": "false"})
        missing = client.post("/agents/modelo-recusado/runs", data={"message": "oi", "stream": "false"})

    assert agents.status_code == 200
    assert sorted(a["id"] for a in agents.json()) == ["valido-1", "valido-2"]
    assert teams.status_code == 200
    assert sorted(t["id"] for t in teams.json()) == ["time-legado", "time-ok"]
    assert {a["name"] for a in agents.json()} == {"Agente valido-1", "Agente valido-2"}
    assert {t["name"] for t in teams.json()} == {"Time time-ok", "Time time-legado"}
    assert config.status_code == 200
    assert sorted(a["id"] for a in config.json()["agents"]) == ["valido-1", "valido-2"]
    assert run.status_code == 200 and run.json()["content"] == "oi"
    assert missing.status_code == 404

    errors = [(r.message, r.context) for r in logger.records if r.level == "error"]
    ignored = [(m, c) for m, c in errors if m.endswith("inválido ignorado")]
    assert ignored == [
        ("Documento de agente inválido ignorado", {"agent_id": None, "mongo_id": None, "error_type": "ValueError"}),
        ("Documento de agente inválido ignorado", {"agent_id": None, "mongo_id": None, "error_type": "ValueError"}),
        (
            "Documento de agente inválido ignorado",
            {"agent_id": "rag-quebrado", "mongo_id": None, "error_type": "AttributeError"},
        ),
        (
            "Documento de agente inválido ignorado",
            {"agent_id": "descricao-objeto", "mongo_id": None, "error_type": "ValueError"},
        ),
        ("Documento de team inválido ignorado", {"team_id": "time-modo", "mongo_id": None, "error_type": "ValueError"}),
        ("Documento de team inválido ignorado", {"team_id": None, "mongo_id": None, "error_type": "ValueError"}),
        (
            "Documento de team inválido ignorado",
            {"team_id": "time-descricao", "mongo_id": None, "error_type": "ValueError"},
        ),
    ]
    assert ("Agente com id repetido ignorado", {"agent_id": "valido-1"}) in errors
    assert ("Team com id repetido ignorado", {"team_id": "time-ok"}) in errors
    # uma falha de criação = um log (só o tipo; o texto da exceção pode ter segredo de SDK)
    assert [m for m, c in errors if c.get("agent_id") == "modelo-recusado"] == ["Agente não carregado"]
    assert not any("error" in c for _, c in errors)
    assert (
        "Agente não carregado",
        {
            "agent_id": "modelo-recusado",
            "error_type": "InvalidModelConfigError",
            # F2-02: a recusa vem do create_model (texto da fábrica); o validate_model_config saiu.
            "reason": "modelo 'modelo-invalido' marcado como inválido no fake",
        },
    ) in errors
    assert ("Team não carregado", {"team_id": "time-sem-membro-ativo", "error_type": "ValueError"}) in errors
    assert all(MARKER not in repr(r) for r in logger.records)
