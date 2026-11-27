"""QA F4-03, smoke com o app real: startup (lifespan + AgentOS) e ``POST /agents/{id}/runs`` por HTTP.

O agente sobe pelo ``AgentFactoryService`` real com a tool HTTP real (``HttpToolFactory`` sobre upstream falso); o
modelo é roteirizado. O evento de auditoria sai com o agente, o usuário e a sessão que o cliente HTTP mandou.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from agno.models.response import ModelResponse
from starlette.testclient import TestClient

from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase
from src.application.use_cases.get_active_teams_use_case import GetActiveTeamsUseCase
from src.domain.entities.tool import HttpMethod, Tool
from src.infrastructure.repositories import mongo_base
from src.infrastructure.repositories.mongo_agent_config_repository import MongoAgentConfigRepository
from src.infrastructure.repositories.mongo_team_config_repository import MongoTeamConfigRepository
from src.infrastructure.runtime.agno import AgnoRuntime
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from src.infrastructure.runtime.agno.team_factory_service import TeamFactoryService
from src.infrastructure.web.app_factory import AppFactory
from src.presentation.controllers.orquestrador_controller import OrquestradorController
from tests.fakes.app_boot import _ENV_NAMES, CONN, LOCAL, _serve_with, agent_doc
from tests.fakes.logger import RecordingLogger
from tests.fakes.models import FakeEmbedderFactory, FakeModelFactory
from tests.fakes.mongo import FakeMongoClient, FakeMongoCollection
from tests.fakes.repositories import InMemoryToolRepository
from tests.fakes.session_db import fake_session_db


def test_run_por_http_gera_evento_de_auditoria_com_o_usuario_e_a_sessao_do_cliente(monkeypatch: pytest.MonkeyPatch):
    logger = RecordingLogger()
    seen: list[httpx.Request] = []

    def upstream(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"uf": "SP"})

    call = ModelResponse(
        role="assistant",
        tool_calls=[{"id": "c0", "type": "function", "function": {"name": "cep", "arguments": json.dumps({})}}],
    )
    models = FakeModelFactory(responses=[call, ModelResponse(role="assistant", content="pronto")])  # type: ignore[list-item]
    client = FakeMongoClient(
        {
            "agents_config": FakeMongoCollection([agent_doc("dev", tools_ids=["cep"])]),
            "teams_config": FakeMongoCollection([]),
        }
    )
    monkeypatch.setattr(mongo_base.MongoClientFactory, "get_client", classmethod(lambda cls, _conn: client))
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    tool = Tool(
        id="cep", name="cep", description="Busca CEP", route="https://api.example.com/cep",
        http_method=HttpMethod.GET, parameters=[],
    )  # fmt: skip
    session_db = fake_session_db()
    runtime = AgnoRuntime(
        agent_factory=AgentFactoryService(
            db_url=CONN,
            session_db=session_db,
            logger=logger,
            model_factory=models,
            embedder_factory=FakeEmbedderFactory(),
            tool_factory=HttpToolFactory(logger, transport=httpx.MockTransport(upstream)),
            tool_repository=InMemoryToolRepository([tool]),
        ),
        team_factory=TeamFactoryService(session_db=session_db, logger=logger, model_factory=models),
        session_db=session_db,
    )
    controller = OrquestradorController(
        get_active_agents_use_case=GetActiveAgentsUseCase(
            runtime, MongoAgentConfigRepository(connection_string=CONN, logger=logger), logger
        ),
        get_active_teams_use_case=GetActiveTeamsUseCase(
            runtime, MongoTeamConfigRepository(connection_string=CONN, logger=logger), logger
        ),
        logger=logger,
    )
    factory = AppFactory()
    app = factory.create_app()
    _serve_with(monkeypatch, factory, controller, runtime)

    with TestClient(app, **LOCAL) as http:  # type: ignore[arg-type]
        response = http.post(
            "/agents/dev/runs",
            data={"message": "cep?", "stream": "false", "user_id": "ana", "session_id": "sess-http"},
        )
        body: dict[str, Any] = response.json()

    assert response.status_code == 200, response.text
    assert len(seen) == 1
    events = [r for r in logger.records if r.context.get("audit") == "tool_call"]
    assert len(events) == 1
    [event] = events
    assert (event.context["agent_id"], event.context["user_id"], event.context["session_id"]) == (
        "dev", "ana", "sess-http",
    )  # fmt: skip
    assert event.context["run_id"] == body["run_id"]
    assert (event.context["source"], event.context["result"], event.context["http_status"]) == ("http", "ok", 200)
