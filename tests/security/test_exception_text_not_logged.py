"""R3 (revisão do F2-04, fechado no F2-07): o texto de exceção de driver/SDK não vai para o log.

A mensagem de um erro do pymongo, do httpx ou de um SDK pode trazer segredo (a URI do Mongo com
credencial, um header, um trecho do documento). Cada ponto que logava ``error=str(exc)`` passou a
logar só ``error_type``. Aqui cada um falha com um erro cuja mensagem carrega um marcador, e o
marcador não pode aparecer em nenhum registro de log (mensagem ou contexto).
"""

from __future__ import annotations

import asyncio
from collections.abc import Awaitable, Callable
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from src.application.services.document_indexing_service import DocumentIndexingService
from src.application.services.search_strategies.hierarchical_search_strategy import HierarchicalSearchStrategy
from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase
from src.application.use_cases.get_active_teams_use_case import GetActiveTeamsUseCase
from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.document_node import DocumentNode
from src.domain.entities.model_config import ModelConfig
from src.domain.entities.rag_config import RagConfig
from src.domain.entities.team_config import TeamConfig
from src.domain.entities.tool import HttpMethod, ParameterType, Tool, ToolParameter
from src.domain.ports import IEmbedderFactory
from src.domain.ports.summary_generator_port import ISummaryGenerator
from src.infrastructure import dependency_injection as di
from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.parsers.text_document_parser import TextDocumentParser
from src.infrastructure.repositories import mongo_base
from src.infrastructure.repositories.mongo_agent_config_repository import MongoAgentConfigRepository
from src.infrastructure.repositories.mongo_document_tree_repository import MongoDocumentTreeRepository
from src.infrastructure.repositories.mongo_team_config_repository import MongoTeamConfigRepository
from src.infrastructure.repositories.mongo_tool_repository import MongoToolRepository
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from src.infrastructure.web import app_factory
from src.presentation.controllers.orquestrador_controller import OrquestradorController
from tests.fakes import (
    FailingMongoClient,
    InMemoryAgentConfigRepository,
    InMemoryDocumentTreeRepository,
    InMemoryTeamConfigRepository,
    RecordingLogger,
)
from tests.fakes.runtime import FakeAgentRuntime
from tests.fakes.startup_world import CLEAN_ENV, Events, startup_world

MARKER = "mongodb://app:s3nh4-do-driver@db.interno.invalid:27017/?authSource=admin"
CONN = "mongodb://mongo.invalid"


class DriverError(Exception):
    """Erro como o de um driver ou SDK: a mensagem cita a URI com credencial."""


def _driver_error() -> DriverError:
    return DriverError(f"connection refused: {MARKER}")


def _assert_only_the_error_type(
    logger: RecordingLogger,
    message: str,
    *,
    level: str = "error",
    error_type: str = "DriverError",
    marker: str = MARKER,
) -> None:
    """O registro do ponto existe com o tipo do erro, sem ``error``, e nenhum registro traz o marcador."""
    assert [r for r in logger.records if marker in repr(r)] == []
    [record] = [r for r in logger.records if r.message == message]
    assert record.level == level
    assert record.context["error_type"] == error_type
    assert "error" not in record.context


# ── application ──────────────────────────────────────────────────────


class _FailingEmbedder:
    def get_embedding(self, text: str) -> list[float]:
        raise _driver_error()


class _FailingEmbedderFactory(IEmbedderFactory):
    def create_embedder(self, config: ModelConfig) -> _FailingEmbedder:
        return _FailingEmbedder()


class _FixedSummary(ISummaryGenerator):
    async def generate_summary(self, content: str) -> str:
        return "resumo"


async def test_indexacao_loga_so_o_tipo_quando_o_embedder_falha() -> None:
    logger = RecordingLogger()
    tree = InMemoryDocumentTreeRepository()
    service = DocumentIndexingService(
        parser=TextDocumentParser(),
        tree_repository=tree,
        summary_generator=_FixedSummary(),
        embedder_factory=_FailingEmbedderFactory(),
        logger=logger,
    )

    nodes = await service.index_document("doc.txt", "conteúdo do documento", RagConfig(active=True, doc_name="doc.txt"))

    assert [node.embedding for node in nodes] == [None]  # indexa sem embedding, como antes
    _assert_only_the_error_type(logger, "Erro ao computar embedding", level="warning")


async def test_busca_hierarquica_loga_so_o_tipo_quando_o_embedder_falha() -> None:
    logger = RecordingLogger()
    strategy = HierarchicalSearchStrategy(
        tree_repository=InMemoryDocumentTreeRepository(),
        embedder=_FailingEmbedder(),
        doc_name="doc.txt",
        logger=logger,
    )

    assert await strategy.search("pergunta") == []
    _assert_only_the_error_type(logger, "Erro ao computar embedding", level="warning")


# ── controller ───────────────────────────────────────────────────────


class _DownAgents(InMemoryAgentConfigRepository):
    async def get_active_agents(self) -> list[AgentConfig]:
        raise _driver_error()


class _DownTeams(InMemoryTeamConfigRepository):
    async def get_active_teams(self) -> list[TeamConfig]:
        raise _driver_error()


def _controller(
    logger: RecordingLogger, agents: InMemoryAgentConfigRepository, teams: InMemoryTeamConfigRepository
) -> OrquestradorController:
    runtime = FakeAgentRuntime()
    return OrquestradorController(
        get_active_agents_use_case=GetActiveAgentsUseCase(runtime, agents, logger),
        get_active_teams_use_case=GetActiveTeamsUseCase(runtime, teams, logger),
        logger=logger,
    )


async def test_controller_loga_so_o_tipo_quando_a_carga_de_agentes_falha() -> None:
    logger = RecordingLogger()
    controller = _controller(logger, _DownAgents(), InMemoryTeamConfigRepository())

    with pytest.raises(DriverError):
        await controller.get_agents()

    _assert_only_the_error_type(logger, "Erro ao carregar agentes")


async def test_controller_loga_so_o_tipo_quando_a_carga_de_teams_falha() -> None:
    logger = RecordingLogger()
    controller = _controller(logger, InMemoryAgentConfigRepository(), _DownTeams())

    assert await controller.get_teams() == []
    _assert_only_the_error_type(logger, "Erro ao carregar teams")


# ── repositórios Mongo ───────────────────────────────────────────────


@pytest.fixture
def mongo_down(monkeypatch: pytest.MonkeyPatch) -> None:
    """Todo repositório Mongo recebe um cliente cujo servidor recusa a conexão."""
    client = FailingMongoClient(_driver_error())
    monkeypatch.setattr(mongo_base.MongoClientFactory, "get_client", classmethod(lambda cls, _conn: client))


RepositoryCall = Callable[[RecordingLogger], Awaitable[Any]]

_NODE = DocumentNode(id="doc.txt::node::0", doc_name="doc.txt", level=0, title="t", content="c")

_REPOSITORY_FAILURES: list[tuple[str, RepositoryCall]] = [
    (
        "Erro ao buscar agentes ativos",
        lambda logger: MongoAgentConfigRepository(connection_string=CONN, logger=logger).get_active_agents(),
    ),
    (
        "Erro ao buscar agente",
        lambda logger: MongoAgentConfigRepository(connection_string=CONN, logger=logger).get_agent_by_id("a1"),
    ),
    (
        "Erro ao buscar teams ativos",
        lambda logger: MongoTeamConfigRepository(connection_string=CONN, logger=logger).get_active_teams(),
    ),
    (
        "Erro ao buscar team",
        lambda logger: MongoTeamConfigRepository(connection_string=CONN, logger=logger).get_team_by_id("t1"),
    ),
    (
        "Erro ao buscar tools por IDs",
        lambda logger: MongoToolRepository(connection_string=CONN, logger=logger).get_tools_by_ids(["x"]),
    ),
    (
        "Erro ao buscar tool",
        lambda logger: MongoToolRepository(connection_string=CONN, logger=logger).get_tool_by_id("x"),
    ),
    (
        "Erro ao listar tools ativas",
        lambda logger: MongoToolRepository(connection_string=CONN, logger=logger).get_all_active_tools(),
    ),
    (
        "Erro ao salvar nós",
        lambda logger: MongoDocumentTreeRepository(connection_string=CONN, logger=logger).save_nodes([_NODE]),
    ),
]


@pytest.mark.usefixtures("mongo_down")
@pytest.mark.parametrize(("message", "call"), _REPOSITORY_FAILURES, ids=[m for m, _ in _REPOSITORY_FAILURES])
async def test_repositorio_mongo_loga_so_o_tipo_e_re_levanta_o_erro_do_driver(
    message: str, call: RepositoryCall
) -> None:
    logger = RecordingLogger()

    with pytest.raises(DriverError):
        await call(logger)

    _assert_only_the_error_type(logger, message)


@pytest.mark.usefixtures("mongo_down")
async def test_ping_do_repositorio_devolve_false_e_loga_so_o_tipo() -> None:
    logger = RecordingLogger()

    assert await MongoToolRepository(connection_string=CONN, logger=logger).ping() is False
    _assert_only_the_error_type(logger, "MongoDB ping falhou")


# ── tools HTTP ───────────────────────────────────────────────────────

_TOOL = Tool(
    id="busca",
    name="Busca",
    description="Busca pedidos",
    route="https://api.example.invalid/pedidos",
    http_method=HttpMethod.GET,
    parameters=[ToolParameter(name="q", type=ParameterType.STRING, description="termo")],
)


def _upstream_raising(monkeypatch: pytest.MonkeyPatch, make_error: Callable[[httpx.Request], Exception]) -> None:
    """``httpx.AsyncClient`` com ``MockTransport`` que levanta o erro dado: nada sai da máquina."""
    real_client: Callable[..., httpx.AsyncClient] = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        raise make_error(request)

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs)
    )


@pytest.mark.parametrize(
    ("make_error", "message", "error_type", "expected_result"),
    [
        (
            lambda request: _driver_error(),
            "Erro inesperado",
            "DriverError",
            "Erro inesperado ao chamar a tool (DriverError)",
        ),
        (
            lambda request: httpx.ConnectError(f"falha: {MARKER}", request=request),
            "Request error",
            "ConnectError",
            "Erro na requisição: falha ao chamar a tool (ConnectError)",
        ),
    ],
    ids=["erro-inesperado", "erro-de-rede"],
)
async def test_tool_http_loga_e_devolve_so_o_tipo_quando_a_chamada_falha(
    monkeypatch: pytest.MonkeyPatch,
    make_error: Callable[[httpx.Request], Exception],
    message: str,
    error_type: str,
    expected_result: str,
) -> None:
    """O resultado da tool vai ao LLM, ao cliente AG-UI, a ``agno_sessions`` e ao span: só texto nosso."""
    _upstream_raising(monkeypatch, make_error)
    logger = RecordingLogger()
    (function,) = await HttpToolFactory(logger=logger).create_tools_from_configs([_TOOL])

    result = await function.entrypoint(q="x")

    assert result == expected_result
    _assert_only_the_error_type(logger, message, error_type=error_type)


async def test_tool_http_com_header_invalido_nao_devolve_o_valor_do_header() -> None:
    """Header com quebra de linha (bloco ``|`` do YAML): o h11 recusa citando o valor inteiro
    (``Illegal header value b'Bearer <segredo>\\n'``). httpx real contra um listener de loopback do
    próprio teste (o ``MockTransport`` não passa pelo h11); nada sai da máquina.
    """
    secret = "s3gr3d0-do-header-QA"  # noqa: S105 - marcador de vazamento, não é segredo

    async def close_at_once(_reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        writer.close()

    logger = RecordingLogger()
    async with await asyncio.start_server(close_at_once, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        tool = Tool(
            id="busca",
            name="Busca",
            description="Busca pedidos",
            route=f"http://127.0.0.1:{port}/pedidos",
            http_method=HttpMethod.GET,
            parameters=[ToolParameter(name="q", type=ParameterType.STRING, description="termo")],
            headers={"Authorization": f"Bearer {secret}\n"},
        )
        (function,) = await HttpToolFactory(logger=logger).create_tools_from_configs([tool])

        result = await function.entrypoint(q="x")

    assert result == "Erro na requisição: falha ao chamar a tool (LocalProtocolError)"
    _assert_only_the_error_type(logger, "Request error", error_type="LocalProtocolError", marker=secret)


async def test_tool_http_com_argumento_de_rota_que_nao_codifica_loga_e_devolve_so_o_tipo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Surrogate solto num parâmetro de rota: o ``quote`` levanta ``UnicodeEncodeError`` ao montar a
    URL, antes da chamada. Sem tratamento nosso a exceção escapava do entrypoint sem log e o agno
    usava ``str(e)`` como resultado da tool (R7 da revisão do F2-07).
    """
    requests: list[httpx.Request] = []
    real_client: Callable[..., httpx.AsyncClient] = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs)
    )
    tool = Tool(
        id="pedido",
        name="Pedido",
        description="Busca um pedido",
        route="https://api.example.invalid/pedidos/{pedido_id}",
        http_method=HttpMethod.GET,
        parameters=[ToolParameter(name="pedido_id", type=ParameterType.STRING, description="id", required=True)],
    )
    logger = RecordingLogger()
    (function,) = await HttpToolFactory(logger=logger).create_tools_from_configs([tool])

    result = await function.entrypoint(pedido_id="\udc80")

    assert result == "Erro inesperado ao chamar a tool (UnicodeEncodeError)"
    assert requests == []  # nada foi chamado
    _assert_only_the_error_type(
        logger, "Erro inesperado", error_type="UnicodeEncodeError", marker="surrogates not allowed"
    )
    assert "\\udc80" not in repr(logger.records)  # nem o valor do argumento


async def test_tool_http_que_nao_pode_ser_criada_loga_so_o_tipo() -> None:
    """Tipo de parâmetro fora do enum (entidade montada sem o mapper): o ``KeyError`` cita o valor."""
    logger = RecordingLogger()
    broken = Tool(
        id="quebrada",
        name="Quebrada",
        description="d",
        route="https://api.example.invalid/x",
        http_method=HttpMethod.GET,
        parameters=[ToolParameter(name="q", type=MARKER, description="termo")],  # type: ignore[arg-type]
    )

    functions = await HttpToolFactory(logger=logger).create_tools_from_configs([broken, _TOOL])

    assert [function.name for function in functions] == ["busca"]  # a tool válida continua sendo criada
    _assert_only_the_error_type(logger, "Erro ao criar ferramenta", error_type="KeyError")


# ── composition root e lifespan ──────────────────────────────────────


@pytest.fixture
def clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in CLEAN_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("OTEL_ENABLED", "false")


class _DownMotor(FailingMongoClient):
    def __init__(self, *_: object, **__: object) -> None:
        super().__init__(_driver_error())


@pytest.mark.usefixtures("clean_env", "mongo_down")
async def test_container_loga_so_o_tipo_quando_o_mongo_esta_fora_no_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    logger = RecordingLogger()
    monkeypatch.setattr(di, "AsyncIOMotorClient", _DownMotor)
    monkeypatch.setattr(di, "StructlogLoggerAdapter", lambda _name: logger)

    container = await di.DependencyContainer.create_async(AppConfig.load())  # sobe sem Mongo
    await container.cleanup()

    _assert_only_the_error_type(logger, "MongoDB não disponível na inicialização", level="warning")
    _assert_only_the_error_type(
        logger, "Não foi possível criar índices da árvore de documentos", level="warning"
    )


@pytest.mark.usefixtures("clean_env")
@pytest.mark.parametrize(
    ("fail_at", "message", "startup_fails"),
    [
        ("entities", "Erro crítico no lifespan", True),
        ("mount", "Erro ao montar AgentOS — continuando com montagem parcial ou sem rotas de agente", False),
    ],
)
async def test_lifespan_loga_so_o_tipo_quando_o_startup_falha(
    monkeypatch: pytest.MonkeyPatch, fail_at: str, message: str, startup_fails: bool
) -> None:
    """``startup_world`` injeta ``RuntimeError("falha injetada: <etapa>")``: o texto dela é o marcador."""
    logger = RecordingLogger()
    monkeypatch.setattr(di, "StructlogLoggerAdapter", lambda _name: logger)
    monkeypatch.setattr(app_factory, "StructlogLoggerAdapter", lambda _name: logger)

    with startup_world(Events(), fail_at=fail_at):
        factory = app_factory.AppFactory()
        app: FastAPI = factory.create_app()
        if startup_fails:
            with pytest.raises(RuntimeError, match="falha injetada"):
                async with app.router.lifespan_context(app):
                    pass  # pragma: no cover - o startup não completa
        else:
            async with app.router.lifespan_context(app):
                pass

    _assert_only_the_error_type(logger, message, error_type="RuntimeError", marker="falha injetada")
