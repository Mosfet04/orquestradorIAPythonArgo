"""Startup real do app com a config em coleções Mongo falsas e as fábricas de modelo dadas.

``boot`` monta ``AppFactory`` + repositórios Mongo reais (sobre ``FakeMongoCollection``) + casos de
uso + ``AgnoRuntime`` com as fábricas reais de agente e team, sobe o lifespan num ``TestClient`` em
loopback e devolve o que o cliente HTTP vê (``/agents``, ``/teams``, ``/config``) e o que foi
logado. Sem ``registry``, modelo e embedder são os fakes; com ele (um ``ProviderRegistry`` ou
qualquer objeto das duas portas), é ele quem cria os dois. Use com a fixture ``offline_knowledge``.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from typing import Any

import pytest
from starlette.testclient import TestClient

from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase
from src.application.use_cases.get_active_teams_use_case import GetActiveTeamsUseCase
from src.domain.ports import IEmbedderFactory, IModelFactory
from src.infrastructure.providers import ProviderRegistry
from src.infrastructure.repositories import mongo_base
from src.infrastructure.repositories.mongo_agent_config_repository import MongoAgentConfigRepository
from src.infrastructure.repositories.mongo_team_config_repository import MongoTeamConfigRepository
from src.infrastructure.runtime.agno import AgnoRuntime, agent_factory_service, team_factory_service
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from src.infrastructure.runtime.agno.team_factory_service import TeamFactoryService
from src.infrastructure.web import app_factory
from src.infrastructure.web.app_factory import AppFactory
from src.presentation.controllers.orquestrador_controller import OrquestradorController
from tests.fakes.logger import RecordingLogger
from tests.fakes.models import FakeEmbedderFactory, FakeModelFactory
from tests.fakes.mongo import FakeMongoClient, FakeMongoCollection
from tests.fakes.repositories import InMemoryToolRepository

CONN = "mongodb://mongo.invalid:27017"
_ENV_NAMES = ("ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS", "APP_HOST", "API_KEY_RUN", "API_KEY_ADMIN")
LOCAL = {"base_url": "http://127.0.0.1:7777", "client": ("127.0.0.1", 50000)}


def agent_doc(agent_id: str, **extra: Any) -> dict[str, Any]:
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


def team_doc(team_id: str, members: list[str], **extra: Any) -> dict[str, Any]:
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
    runtime = AgnoRuntime(
        agent_factory=AgentFactoryService(
            db_url=CONN,
            logger=logger,
            model_factory=models,
            embedder_factory=embedders,
            tool_factory=HttpToolFactory(logger=logger),
            tool_repository=InMemoryToolRepository(),
        ),
        team_factory=TeamFactoryService(db_url=CONN, logger=logger, model_factory=models),
    )
    agents_uc = GetActiveAgentsUseCase(
        runtime,
        MongoAgentConfigRepository(connection_string=CONN, logger=logger),
        logger,
    )
    teams_uc = GetActiveTeamsUseCase(
        runtime,
        MongoTeamConfigRepository(connection_string=CONN, logger=logger),
        logger,
    )
    controller = OrquestradorController(
        get_active_agents_use_case=agents_uc, get_active_teams_use_case=teams_uc, logger=logger
    )
    factory = AppFactory()
    app = factory.create_app()

    async def ensure_container() -> Any:
        async def cleanup() -> None:
            return None

        factory._container = SimpleNamespace(  # type: ignore[assignment]
            config=factory._config,
            cleanup=cleanup,
            health_service=None,
            get_orquestrador_controller=lambda: controller,
            get_agent_runtime=lambda: runtime,
        )
        return factory._container

    monkeypatch.setattr(factory, "_ensure_container", ensure_container)
    monkeypatch.setattr(app_factory, "setup_telemetry", lambda config: None)
    monkeypatch.setattr(app_factory, "shutdown_telemetry", lambda: None)
    with TestClient(app, **LOCAL) as http:  # type: ignore[arg-type]
        agents, teams, config = http.get("/agents"), http.get("/teams"), http.get("/config")
        result.status = (agents.status_code, teams.status_code, config.status_code)
        result.agents, result.teams, result.config = agents.json(), teams.json(), config.json()
    return result
