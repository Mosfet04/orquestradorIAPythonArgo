"""QA do F2-09: o refactor de ``DependencyContainer._initialize`` (``_connect_mongo``,
``_build_hierarchical_rag``, ``_build_runtime``) não mudou a ordem das etapas, o que cada uma recebe
da anterior nem o que ``create_async`` fecha quando uma delas falha.

Container real sobre o ``startup_world`` (só o Mongo e o modelo são falsos). Os construtores do
composition root são embrulhados por espiões que repassam à implementação real: a ordem e os
argumentos observados são os da produção.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

from src.infrastructure import dependency_injection as di
from src.infrastructure.config.app_config import AppConfig
from tests.fakes.startup_world import CLEAN_ENV, Events, startup_world

# Ordem do wiring na base (b8440e1): cliente motor, fábrica de tools, repositórios de config,
# RAG hierárquico, db de sessões, fábricas, runtime, casos de uso, controller.
STAGES = [
    "AsyncIOMotorClient",
    "HttpToolFactory",
    "MongoAgentConfigRepository",
    "MongoTeamConfigRepository",
    "MongoToolRepository",
    "MongoDocumentTreeRepository",
    "LLMSummaryGenerator",
    "DocumentIndexingService",
    "KnowledgeSearchFactory",
    "create_session_db",
    "AgentFactoryService",
    "TeamFactoryService",
    "AgnoRuntime",
    "GetActiveAgentsUseCase",
    "GetActiveTeamsUseCase",
    "OrquestradorController",
]
# Etapas depois de o db de sessões existir: a falha nelas tem de fechá-lo.
AFTER_SESSION_DB = STAGES[STAGES.index("create_session_db") + 1 :]


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in CLEAN_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")


@dataclass
class Call:
    name: str
    args: tuple[Any, ...]
    kwargs: dict[str, Any]
    result: Any = None


@dataclass
class Spies:
    calls: list[Call] = field(default_factory=list)

    def names(self) -> list[str]:
        return [c.name for c in self.calls]

    def only(self, name: str) -> Call:
        (call,) = [c for c in self.calls if c.name == name]
        return call


def _spy(monkeypatch: pytest.MonkeyPatch, spies: Spies, name: str, *, fail: Exception | None = None) -> None:
    original: Callable[..., Any] = getattr(di, name)

    def wrapper(*args: Any, **kwargs: Any) -> Any:
        call = Call(name, args, kwargs)
        spies.calls.append(call)
        if fail is not None:
            raise fail
        call.result = original(*args, **kwargs)
        return call.result

    monkeypatch.setattr(di, name, wrapper)


def _spy_all(monkeypatch: pytest.MonkeyPatch, *, fail_at: str | None = None, exc: Exception | None = None) -> Spies:
    spies = Spies()
    for name in STAGES:
        _spy(monkeypatch, spies, name, fail=exc if name == fail_at else None)
    return spies


async def test_etapas_rodam_na_ordem_da_base_e_cada_uma_recebe_o_que_a_anterior_devolveu(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = Events()
    with startup_world(events):
        spies = _spy_all(monkeypatch)
        config = AppConfig.load()
        container = await di.DependencyContainer.create_async(config)
        try:
            assert spies.names() == STAGES

            tree_repo = spies.only("MongoDocumentTreeRepository").result
            summary = spies.only("LLMSummaryGenerator").result
            indexing = spies.only("DocumentIndexingService").result
            search = spies.only("KnowledgeSearchFactory").result
            tool_repo = spies.only("MongoToolRepository").result
            session_db = spies.only("create_session_db").result
            agent_factory = spies.only("AgentFactoryService").result
            team_factory = spies.only("TeamFactoryService").result
            runtime = spies.only("AgnoRuntime").result
            registry = spies.only("TeamFactoryService").kwargs["model_factory"]

            tree_kwargs = spies.only("MongoDocumentTreeRepository").kwargs
            assert tree_kwargs["connection_string"] == config.mongo_connection_string
            assert tree_kwargs["database_name"] == config.mongo_database_name
            assert spies.only("LLMSummaryGenerator").kwargs["model_factory"] is registry
            index_kwargs = spies.only("DocumentIndexingService").kwargs
            assert index_kwargs["tree_repository"] is tree_repo
            assert index_kwargs["summary_generator"] is summary
            assert index_kwargs["embedder_factory"] is registry
            assert spies.only("KnowledgeSearchFactory").kwargs["tree_repository"] is tree_repo

            assert spies.only("create_session_db").args == (config.mongo_connection_string, config.mongo_database_name)
            agent_kwargs = spies.only("AgentFactoryService").kwargs
            assert agent_kwargs["session_db"] is session_db
            assert agent_kwargs["indexing_service"] is indexing
            assert agent_kwargs["search_factory"] is search
            assert agent_kwargs["tool_repository"] is tool_repo
            assert agent_kwargs["tool_factory"] is spies.only("HttpToolFactory").result
            assert agent_kwargs["model_factory"] is registry and agent_kwargs["embedder_factory"] is registry
            assert (agent_kwargs["db_url"], agent_kwargs["db_name"]) == (
                config.mongo_connection_string,
                config.mongo_database_name,
            )
            assert spies.only("TeamFactoryService").kwargs["session_db"] is session_db
            runtime_kwargs = spies.only("AgnoRuntime").kwargs
            assert runtime_kwargs == {
                "agent_factory": agent_factory,
                "team_factory": team_factory,
                "session_db": session_db,
            }

            agents_repo = spies.only("MongoAgentConfigRepository").result
            teams_repo = spies.only("MongoTeamConfigRepository").result
            assert spies.only("GetActiveAgentsUseCase").args[:2] == (runtime, agents_repo)
            assert spies.only("GetActiveTeamsUseCase").args[:2] == (runtime, teams_repo)
            assert container.get_agent_runtime() is runtime
            assert container.get_orquestrador_controller() is spies.only("OrquestradorController").result
            assert container.health_service is not None
        finally:
            await container.cleanup()


@pytest.mark.parametrize("stage", STAGES[1:])
async def test_falha_em_qualquer_etapa_depois_do_mongo_fecha_o_cliente_e_o_db_de_sessoes_so_se_ja_existe(
    monkeypatch: pytest.MonkeyPatch, stage: str
) -> None:
    events = Events()
    with startup_world(events) as motors:
        spies = _spy_all(monkeypatch, fail_at=stage, exc=RuntimeError(f"falha em {stage}"))
        with pytest.raises(RuntimeError, match=f"falha em {stage}"):
            await di.DependencyContainer.create_async(AppConfig.load())

    assert spies.names() == STAGES[: STAGES.index(stage) + 1]  # nada roda depois da etapa que falhou
    assert [m.closes for m in motors] == [1]
    db_exists = stage in AFTER_SESSION_DB
    assert (events.count("db:created"), events.count("db:close")) == ((1, 1) if db_exists else (0, 0))
    assert not [n for n in events.names if n.endswith(":on-loop")]


async def test_falha_ao_criar_o_db_de_sessoes_fecha_o_cliente_do_container(monkeypatch: pytest.MonkeyPatch) -> None:
    """``create_session_db`` é a única etapa que atribui ``_session_db``: se ela falha não há db a fechar."""
    events = Events()
    with startup_world(events) as motors:
        _spy_all(monkeypatch, fail_at="create_session_db", exc=OSError("sem cliente"))
        with pytest.raises(OSError, match="sem cliente"):
            await di.DependencyContainer.create_async(AppConfig.load())

    assert [m.closes for m in motors] == [1]
    assert events.count("db:close") == 0


@pytest.mark.parametrize("exc_type", [asyncio.CancelledError, KeyboardInterrupt])
@pytest.mark.parametrize("stage", ["MongoDocumentTreeRepository", "create_session_db", "AgnoRuntime"])
async def test_cancelamento_nas_etapas_novas_fecha_o_que_abriu_e_propaga(
    monkeypatch: pytest.MonkeyPatch, stage: str, exc_type: type[BaseException]
) -> None:
    events = Events()
    with startup_world(events) as motors:

        def interrupted(*_: Any, **__: Any) -> Any:
            raise exc_type()

        monkeypatch.setattr(di, stage, interrupted)
        with pytest.raises(exc_type):
            await di.DependencyContainer.create_async(AppConfig.load())

    assert [m.closes for m in motors] == [1]
    assert events.count("db:close") == events.count("db:created")


async def test_ping_cancelado_fecha_o_cliente_ja_criado_e_propaga(monkeypatch: pytest.MonkeyPatch) -> None:
    events = Events()
    with startup_world(events) as motors:
        new_motor = di.AsyncIOMotorClient

        def motor_with_cancelled_ping(*args: Any, **kwargs: Any) -> Any:
            motor = new_motor(*args, **kwargs)

            async def command(*_: object) -> dict[str, int]:
                raise asyncio.CancelledError

            motor.command = command
            return motor

        monkeypatch.setattr(di, "AsyncIOMotorClient", motor_with_cancelled_ping)
        with pytest.raises(asyncio.CancelledError):
            await di.DependencyContainer.create_async(AppConfig.load())

    assert [m.closes for m in motors] == [1]
    assert events.count("db:created") == 0


async def test_mongo_fora_no_ping_nao_impede_o_startup_e_o_health_service_existe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events = Events()
    with startup_world(events) as motors:
        spies = _spy_all(monkeypatch)
        new_motor = di.AsyncIOMotorClient

        def motor_without_server(*args: Any, **kwargs: Any) -> Any:
            motor = new_motor(*args, **kwargs)

            async def command(*_: object) -> dict[str, int]:
                raise ConnectionError("mongo fora")

            motor.command = command
            return motor

        monkeypatch.setattr(di, "AsyncIOMotorClient", motor_without_server)
        container = await di.DependencyContainer.create_async(AppConfig.load())
        try:
            assert container.health_service is not None
            assert [c.name for c in spies.calls if c.name != "AsyncIOMotorClient"] == STAGES[1:]
            assert [m.closes for m in motors] == [0]
        finally:
            await container.cleanup()
