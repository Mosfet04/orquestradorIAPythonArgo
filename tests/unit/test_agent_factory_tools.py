"""Tools de um agente (F1-05): tool referenciada e ausente/inválida não some em silêncio."""

from __future__ import annotations

from typing import Any

import pytest

from src.application.services import agent_factory_service
from src.application.services.agent_factory_service import AgentFactoryService
from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.tool import HttpMethod, Tool
from src.domain.ports import IToolFactory
from src.domain.repositories.tool_repository import IToolRepository
from src.infrastructure.http.http_tool_factory import HttpToolFactory
from tests.fakes import FakeEmbedderFactory, FakeModelFactory, InMemoryToolRepository, RecordingLogger


def _tool(tool_id: str, *, active: bool = True) -> Tool:
    return Tool(
        id=tool_id,
        name=tool_id,
        description=f"Tool {tool_id}",
        route=f"https://api.example.invalid/{tool_id}",
        http_method=HttpMethod.GET,
        parameters=[],
        active=active,
    )


def _config(*tools_ids: str) -> AgentConfig:
    return AgentConfig(
        id="agente-x",
        nome="Agente X",
        factory_ia_model="openai",
        model="gpt-4o-mini",
        descricao="desc",
        prompt="prompt",
        tools_ids=list(tools_ids),
    )


@pytest.fixture(autouse=True)
def no_mongo(monkeypatch: pytest.MonkeyPatch) -> None:
    """O ``MongoDb`` do agno criaria um ``MongoClient`` real; o agente funciona sem db."""
    monkeypatch.setattr(agent_factory_service, "MongoAgentDb", lambda **_: None)


def _service(
    logger: RecordingLogger,
    repository: IToolRepository,
    tool_factory: IToolFactory | None = None,
) -> AgentFactoryService:
    return AgentFactoryService(
        db_url="mongodb://mongo.invalid:27017",
        logger=logger,
        model_factory=FakeModelFactory(),
        embedder_factory=FakeEmbedderFactory(),
        tool_factory=tool_factory or HttpToolFactory(logger=logger),
        tool_repository=repository,
    )


def _errors(logger: RecordingLogger) -> list[tuple[str, dict[str, Any]]]:
    return [(r.message, r.context) for r in logger.records if r.level == "error"]


async def test_tool_ausente_ou_inativa_gera_log_de_erro_com_ids_e_o_agente_sobe_com_as_demais():
    logger = RecordingLogger()
    repository = InMemoryToolRepository([_tool("ok"), _tool("desligada", active=False)])

    agent = await _service(logger, repository).create_agent(_config("ok", "inexistente", "desligada"))

    assert [t.name for t in agent.tools] == ["ok"]
    assert [ctx for msg, ctx in _errors(logger)] == [
        {"agent_id": "agente-x", "tool_id": "inexistente"},
        {"agent_id": "agente-x", "tool_id": "desligada"},
    ]
    assert all("não encontrada" in msg for msg, _ in _errors(logger))


async def test_todas_as_tools_encontradas_nao_geram_erro():
    logger = RecordingLogger()

    agent = await _service(logger, InMemoryToolRepository([_tool("a"), _tool("b")])).create_agent(_config("a", "b"))

    assert [t.name for t in agent.tools] == ["a", "b"]
    assert _errors(logger) == []


class _FailingRepository(InMemoryToolRepository):
    async def get_tools_by_ids(self, tool_ids: list[str]) -> list[Tool]:
        raise RuntimeError("mongo fora")


async def test_falha_ao_buscar_tools_gera_log_de_erro_com_agente_e_ids():
    logger = RecordingLogger()

    agent = await _service(logger, _FailingRepository()).create_agent(_config("a", "b"))

    assert agent.tools == []
    assert _errors(logger) == [
        (
            "Erro ao buscar tools do agente; agente sobe sem elas",
            {"agent_id": "agente-x", "tool_ids": ["a", "b"], "error_type": "RuntimeError"},
        )
    ]


class _RejectingFactory(IToolFactory):
    """Factory que não consegue criar a tool ``ruim`` (ex.: tipo de parâmetro inválido)."""

    def __init__(self, inner: IToolFactory) -> None:
        self._inner = inner

    async def create_tools_from_configs(self, tools: list[Tool]) -> list[Any]:
        return await self._inner.create_tools_from_configs([t for t in tools if t.id != "ruim"])


async def test_tool_que_a_factory_nao_cria_gera_log_de_erro_com_ids():
    logger = RecordingLogger()
    factory = _RejectingFactory(HttpToolFactory(logger=logger))
    repository = InMemoryToolRepository([_tool("ok"), _tool("ruim")])

    agent = await _service(logger, repository, factory).create_agent(_config("ruim", "ok"))

    assert [t.name for t in agent.tools] == ["ok"]
    assert [ctx for _, ctx in _errors(logger)] == [{"agent_id": "agente-x", "tool_id": "ruim"}]


class _ExplodingFactory(IToolFactory):
    """Implementação de ``IToolFactory`` que levanta (a porta não promete não levantar)."""

    async def create_tools_from_configs(self, tools: list[Tool]) -> list[Any]:
        raise RuntimeError("factory quebrou")


async def test_factory_que_levanta_gera_log_de_erro_e_o_agente_sobe_sem_a_tool():
    logger = RecordingLogger()
    repository = InMemoryToolRepository([_tool("a")])

    agent = await _service(logger, repository, _ExplodingFactory()).create_agent(_config("a"))

    assert agent.tools == []
    assert _errors(logger) == [
        (
            "Erro ao criar tool do agente; agente sobe sem ela",
            {"agent_id": "agente-x", "tool_id": "a", "error_type": "RuntimeError"},
        )
    ]
