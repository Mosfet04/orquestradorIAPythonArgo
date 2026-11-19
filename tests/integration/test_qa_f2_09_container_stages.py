"""QA do F2-09: o refactor de ``DependencyContainer._initialize`` (``_connect_mongo``,
``_build_hierarchical_rag``, ``_build_runtime``) não mudou a ordem das etapas, o que cada uma recebe
da anterior nem o que ``create_async`` fecha quando uma delas falha.

Container real sobre o ``startup_world`` (só o Mongo e o modelo são falsos). Os construtores do
composition root são embrulhados por espiões que repassam à implementação real: a ordem e os
argumentos observados são os da produção.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from typing import Any

import pytest

from src.infrastructure import dependency_injection as di
from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.repositories import mongo_base
from src.infrastructure.runtime.agno.embedder_dimensions import DimensionResolvingEmbedderFactory
from src.infrastructure.runtime.agno.session_db import create_session_db
from tests.fakes.startup_world import CLEAN_ENV, Events, _AppMongoClient, startup_world

# Ordem do wiring na base (b8440e1), depois do cliente Mongo do app (``MongoClientFactory``, F3-09b): fábrica
# de tools, repositórios de config, RAG hierárquico, db de sessões, fábricas, runtime, casos de uso, controller.
STAGES = [
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


@contextmanager
def _spied_world(events: Events) -> Iterator[tuple[list[_AppMongoClient], pytest.MonkeyPatch]]:
    """``startup_world`` com um ``MonkeyPatch`` para os espiões, desfeito antes do mundo (F3-09b).

    O espião de ``create_session_db`` embrulha o falso do mundo; desfeito depois dele (pelo
    ``monkeypatch`` do teste, no teardown), devolveria o falso ao ``di`` para o resto do processo.
    """
    with startup_world(events) as clients, pytest.MonkeyPatch.context() as monkeypatch:
        yield clients, monkeypatch


def _spy_all(monkeypatch: pytest.MonkeyPatch, *, fail_at: str | None = None, exc: Exception | None = None) -> Spies:
    spies = Spies()
    for name in STAGES:
        _spy(monkeypatch, spies, name, fail=exc if name == fail_at else None)
    return spies


def _assert_rag_wiring(spies: Spies, config: AppConfig, registry: object) -> object:
    """Árvore, resumo, indexação e busca recebem o que a etapa anterior devolveu; devolve o embedder."""
    tree_repo = spies.only("MongoDocumentTreeRepository").result
    tree_kwargs = spies.only("MongoDocumentTreeRepository").kwargs
    assert tree_kwargs["connection_string"] == config.mongo_connection_string
    assert tree_kwargs["database_name"] == config.mongo_database_name
    assert spies.only("LLMSummaryGenerator").kwargs["model_factory"] is registry
    index_kwargs = spies.only("DocumentIndexingService").kwargs
    assert index_kwargs["tree_repository"] is tree_repo
    assert index_kwargs["summary_generator"] is spies.only("LLMSummaryGenerator").result
    # F2-11: o embedder sai do registry com a dimensão real (sonda), o mesmo para os dois RAGs
    embedders = index_kwargs["embedder_factory"]
    assert isinstance(embedders, DimensionResolvingEmbedderFactory)
    assert embedders.inner is registry  # delega ao registry, que monta o embedder
    assert spies.only("KnowledgeSearchFactory").kwargs["tree_repository"] is tree_repo
    return embedders


def _assert_agent_wiring(spies: Spies, config: AppConfig, registry: object, embedders: object) -> None:
    """Fábricas de agente/team e runtime recebem o db de sessões, os serviços e as fábricas certas."""
    session_db = spies.only("create_session_db").result
    assert spies.only("create_session_db").args == (config.mongo_connection_string, config.mongo_database_name)
    agent_kwargs = spies.only("AgentFactoryService").kwargs
    assert agent_kwargs["session_db"] is session_db
    assert agent_kwargs["indexing_service"] is spies.only("DocumentIndexingService").result
    assert agent_kwargs["search_factory"] is spies.only("KnowledgeSearchFactory").result
    assert agent_kwargs["tool_repository"] is spies.only("MongoToolRepository").result
    assert agent_kwargs["tool_factory"] is spies.only("HttpToolFactory").result
    assert agent_kwargs["model_factory"] is registry and agent_kwargs["embedder_factory"] is embedders
    assert (agent_kwargs["db_url"], agent_kwargs["db_name"]) == (
        config.mongo_connection_string,
        config.mongo_database_name,
    )
    assert spies.only("TeamFactoryService").kwargs["session_db"] is session_db
    assert spies.only("AgnoRuntime").kwargs == {
        "agent_factory": spies.only("AgentFactoryService").result,
        "team_factory": spies.only("TeamFactoryService").result,
        "session_db": session_db,
    }


async def test_etapas_rodam_na_ordem_da_base_e_cada_uma_recebe_o_que_a_anterior_devolveu() -> None:
    events = Events()
    with _spied_world(events) as (_, monkeypatch):
        spies = _spy_all(monkeypatch)
        config = AppConfig.load()
        container = await di.DependencyContainer.create_async(config)
        try:
            assert spies.names() == STAGES
            registry = spies.only("TeamFactoryService").kwargs["model_factory"]
            embedders = _assert_rag_wiring(spies, config, registry)
            _assert_agent_wiring(spies, config, registry, embedders)

            runtime = spies.only("AgnoRuntime").result
            agents_repo = spies.only("MongoAgentConfigRepository").result
            teams_repo = spies.only("MongoTeamConfigRepository").result
            assert spies.only("GetActiveAgentsUseCase").args[:2] == (runtime, agents_repo)
            assert spies.only("GetActiveTeamsUseCase").args[:2] == (runtime, teams_repo)
            assert container.get_agent_runtime() is runtime
            assert container.get_orquestrador_controller() is spies.only("OrquestradorController").result
            assert container.health_service is not None
        finally:
            await container.cleanup()


@pytest.mark.parametrize("stage", STAGES)
async def test_falha_em_qualquer_etapa_depois_do_mongo_fecha_o_cliente_e_o_db_de_sessoes_so_se_ja_existe(
    stage: str,
) -> None:
    events = Events()
    with _spied_world(events) as (clients, monkeypatch):
        spies = _spy_all(monkeypatch, fail_at=stage, exc=RuntimeError(f"falha em {stage}"))
        with pytest.raises(RuntimeError, match=f"falha em {stage}"):
            await di.DependencyContainer.create_async(AppConfig.load())

    assert spies.names() == STAGES[: STAGES.index(stage) + 1]  # nada roda depois da etapa que falhou
    assert [m.closes for m in clients] == [[True]]
    db_exists = stage in AFTER_SESSION_DB
    assert (events.count("db:created"), events.count("db:close")) == ((1, 1) if db_exists else (0, 0))
    assert not [n for n in events.names if n.endswith(":on-loop")]


async def test_falha_ao_criar_o_db_de_sessoes_fecha_o_cliente_do_container() -> None:
    """``create_session_db`` é a única etapa que atribui ``_session_db``: se ela falha não há db a fechar."""
    events = Events()
    with _spied_world(events) as (clients, monkeypatch):
        _spy_all(monkeypatch, fail_at="create_session_db", exc=OSError("sem cliente"))
        with pytest.raises(OSError, match="sem cliente"):
            await di.DependencyContainer.create_async(AppConfig.load())

    assert [m.closes for m in clients] == [[True]]
    assert events.count("db:close") == 0


@pytest.mark.parametrize("exc_type", [asyncio.CancelledError, KeyboardInterrupt])
@pytest.mark.parametrize("stage", ["MongoDocumentTreeRepository", "create_session_db", "AgnoRuntime"])
async def test_cancelamento_nas_etapas_novas_fecha_o_que_abriu_e_propaga(
    stage: str, exc_type: type[BaseException]
) -> None:
    events = Events()
    with _spied_world(events) as (clients, monkeypatch):

        def interrupted(*_: Any, **__: Any) -> Any:
            raise exc_type()

        monkeypatch.setattr(di, stage, interrupted)
        with pytest.raises(exc_type):
            await di.DependencyContainer.create_async(AppConfig.load())

    assert [m.closes for m in clients] == [[True]]
    assert events.count("db:close") == events.count("db:created")


async def test_ping_cancelado_fecha_o_cliente_ja_criado_e_propaga(monkeypatch: pytest.MonkeyPatch) -> None:
    events = Events()
    with startup_world(events) as clients:
        async def command(*_: object) -> dict[str, int]:
            raise asyncio.CancelledError

        monkeypatch.setattr(mongo_base.MongoClientFactory.get_client("qualquer"), "command", command)
        with pytest.raises(asyncio.CancelledError):
            await di.DependencyContainer.create_async(AppConfig.load())

    assert [m.closes for m in clients] == [[True]]
    assert events.count("db:created") == 0


async def test_mongo_fora_no_ping_nao_impede_o_startup_e_o_health_service_existe() -> None:
    events = Events()
    with _spied_world(events) as (clients, monkeypatch):
        spies = _spy_all(monkeypatch)
        async def command(*_: object) -> dict[str, int]:
            raise ConnectionError("mongo fora")

        monkeypatch.setattr(mongo_base.MongoClientFactory.get_client("qualquer"), "command", command)
        container = await di.DependencyContainer.create_async(AppConfig.load())
        try:
            assert container.health_service is not None
            assert spies.names() == STAGES
            assert [m.closes for m in clients] == [[]]
        finally:
            await container.cleanup()


async def test_espioes_do_startup_saem_antes_do_mundo_falso_e_o_di_volta_ao_db_de_sessoes_real() -> None:
    """Regressão (F3-09b): espião sobre um nome que o ``startup_world`` já trocou sai antes dele.

    Desfeito depois do mundo (o ``monkeypatch`` do teste, no teardown), o espião guardava o
    ``create_session_db`` falso como original e o devolvia ao ``di``: os testes seguintes do mesmo
    processo montavam o db de sessões falso (cujo ``close`` não fecha ``AsyncMongoClient`` nenhum).
    """
    with pytest.MonkeyPatch.context() as guard:
        # Rede: se a regressão voltar, o resto do processo continua vendo o real.
        guard.setattr(di, "create_session_db", create_session_db)
        with _spied_world(Events()) as (_, monkeypatch):
            _spy_all(monkeypatch)
            container = await di.DependencyContainer.create_async(AppConfig.load())
            await container.cleanup()
        leaked = vars(di)["create_session_db"]

    assert leaked is create_session_db
