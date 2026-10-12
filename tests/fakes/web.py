"""Cliente HTTP de teste que se apresenta como acesso local (loopback) e montagem do runtime no app.

No modo dev local (sem ``API_KEY_RUN``/``API_KEY_ADMIN``) a auth da borda só libera
request com cliente, servidor e ``Host`` em loopback; o ``TestClient`` padrão usa
``testclient``/``testserver``, que não são loopback.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

from agno.agent import Agent
from agno.db.base import AsyncBaseDb
from agno.team import Team
from fastapi import FastAPI
from starlette.testclient import TestClient
from starlette.types import ASGIApp

from src.infrastructure.runtime.agno import AgnoRuntime
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from src.infrastructure.runtime.agno.team_factory_service import TeamFactoryService
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes.logger import RecordingLogger
from tests.fakes.models import FakeEmbedderFactory, FakeModelFactory
from tests.fakes.repositories import InMemoryToolRepository

LOOPBACK_BASE_URL = "http://127.0.0.1:7777"
LOOPBACK_CLIENT = ("127.0.0.1", 50000)
_RUNTIME_DB_URL = "mongodb://mongo.test.invalid:27017"


def loopback_client(app: ASGIApp, **kwargs: Any) -> TestClient:
    """``TestClient`` com cliente, servidor e ``Host`` em 127.0.0.1."""
    kwargs.setdefault("base_url", LOOPBACK_BASE_URL)
    kwargs.setdefault("client", LOOPBACK_CLIENT)
    return TestClient(app, **kwargs)


def agno_runtime(session_db: AsyncBaseDb | None = None) -> AgnoRuntime:
    """``AgnoRuntime`` real com fábricas de modelo, embedder e tools falsas (construtores sem I/O).

    Para montar entidades já criadas pelo teste: ``mount`` não usa as fábricas. ``session_db`` é o
    ``db=`` do AgentOS (em produção, o db das entidades); sem ele o AgentOS sobe sem db, como as
    entidades do teste.
    """
    logger = RecordingLogger()
    models = FakeModelFactory()
    db = session_db
    return AgnoRuntime(
        agent_factory=AgentFactoryService(
            db_url=_RUNTIME_DB_URL,
            session_db=db,
            logger=logger,
            model_factory=models,
            embedder_factory=FakeEmbedderFactory(),
            tool_factory=HttpToolFactory(logger=logger),
            tool_repository=InMemoryToolRepository(),
        ),
        team_factory=TeamFactoryService(session_db=db, logger=logger, model_factory=models),
        session_db=db,
    )


def mount_agent_os(
    factory: AppFactory,
    app: FastAPI,
    agents: Sequence[Agent | Any],
    teams: Sequence[Team | Any],
    *,
    session_db: AsyncBaseDb | None = None,
) -> AgnoRuntime:
    """O passo de montagem do lifespan (``AppFactory._mount_runtime``) sem Mongo nem container.

    Monta só as rotas (os lifespans do AgentOS ficam para ``runtime.start()``, que o lifespan do
    app chama); devolve o runtime para quem quiser abri-los ou fechá-los. O ``db=`` do AgentOS é
    ``session_db``; sem ele, o db (assíncrono) da primeira entidade que tiver um, como em produção,
    onde é o mesmo objeto. Sem nenhum, o AgentOS sobe sem db (``/schedules`` e ``/approvals`` ficam
    stubs e nenhuma entidade ganha persistência): quem testa a tabela de rotas de produção passa um
    ``fake_session_db()``.
    """
    if session_db is None:
        session_db = next((e.db for e in (*agents, *teams) if isinstance(getattr(e, "db", None), AsyncBaseDb)), None)
    runtime = agno_runtime(session_db)
    factory._mount_runtime(app, runtime, agents, teams)
    return runtime
