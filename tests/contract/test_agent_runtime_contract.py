"""Contrato da porta ``AgentRuntime`` (F2-04): mesma suíte para o fake e para o ``AgnoRuntime``.

A porta tem uma implementação real só (o agno; é a fronteira do framework). O fake de
``tests/fakes/runtime.py`` é a base dos testes dos casos de uso, então obedece ao mesmo
contrato. O ``AgnoRuntime`` roda com fábricas falsas de modelo/embedder e sem Mongo.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass

import pytest

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.team_config import TeamConfig
from src.domain.ports import AgentRuntime, InvalidModelConfigError
from src.infrastructure.runtime.agno import AgnoRuntime, agent_factory_service, team_factory_service
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from src.infrastructure.runtime.agno.team_factory_service import TeamFactoryService
from tests.fakes import FakeEmbedderFactory, FakeModelFactory, InMemoryToolRepository, RecordingLogger
from tests.fakes.runtime import FakeAgentRuntime

REFUSED_MODEL = "recusado"


def _agent(agent_id: str, model: str = "modelo-x") -> AgentConfig:
    return AgentConfig(
        id=agent_id, nome=f"Agente {agent_id}", factory_ia_model="openai", model=model, descricao="d", prompt="p"
    )


def _team(team_id: str, members: list[str], model: str = "modelo-x") -> TeamConfig:
    return TeamConfig(
        id=team_id, nome=f"Team {team_id}", factory_ia_model="openai", model=model, member_ids=members, mode="route"
    )


@dataclass(frozen=True)
class Implementation:
    runtime: AgentRuntime
    refused_agent: AgentConfig
    refused_team: TeamConfig


def _fake(monkeypatch: pytest.MonkeyPatch) -> Implementation:
    refused = {"agente-recusado": InvalidModelConfigError("x"), "team-recusado": InvalidModelConfigError("x")}
    return Implementation(
        runtime=FakeAgentRuntime(failures=refused),
        refused_agent=_agent("agente-recusado"),
        refused_team=_team("team-recusado", ["a1"]),
    )


def _agno(monkeypatch: pytest.MonkeyPatch) -> Implementation:
    monkeypatch.setattr(agent_factory_service, "MongoAgentDb", lambda **_: None)
    monkeypatch.setattr(team_factory_service, "MongoAgentDb", lambda **_: None)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    logger = RecordingLogger()
    models = FakeModelFactory(invalid_models={REFUSED_MODEL})
    runtime = AgnoRuntime(
        agent_factory=AgentFactoryService(
            db_url="mongodb://mongo.contrato.invalid:27017",
            logger=logger,
            model_factory=models,
            embedder_factory=FakeEmbedderFactory(),
            tool_factory=HttpToolFactory(logger=logger),
            tool_repository=InMemoryToolRepository(),
        ),
        team_factory=TeamFactoryService(
            db_url="mongodb://mongo.contrato.invalid:27017", logger=logger, model_factory=models
        ),
    )
    return Implementation(
        runtime=runtime,
        refused_agent=_agent("agente-recusado", model=REFUSED_MODEL),
        refused_team=_team("team-recusado", ["a1"], model=REFUSED_MODEL),
    )


@pytest.fixture(params=[_fake, _agno], ids=["fake", "agno"])
def impl(request: pytest.FixtureRequest, monkeypatch: pytest.MonkeyPatch) -> Implementation:
    factory: Callable[[pytest.MonkeyPatch], Implementation] = request.param
    return factory(monkeypatch)


async def test_agente_montado_tem_o_id_da_config(impl: Implementation):
    agent = await impl.runtime.build_agent(_agent("a1"))

    assert agent.id == "a1"


async def test_team_montado_em_thread_com_agentes_do_runtime_tem_o_id_da_config(impl: Implementation):
    agents = [await impl.runtime.build_agent(_agent("a1")), await impl.runtime.build_agent(_agent("a2"))]

    team = await asyncio.to_thread(impl.runtime.build_team, _team("t1", ["a1"]), agents)

    assert team.id == "t1"


async def test_team_sem_membro_valido_falha(impl: Implementation):
    """Nenhum id de ``member_ids`` entre os agentes dados: falha (o caso de uso isola e loga)."""
    agents = [await impl.runtime.build_agent(_agent("a1"))]

    with pytest.raises(ValueError, match="nenhum membro"):
        await asyncio.to_thread(impl.runtime.build_team, _team("t1", ["inexistente"]), agents)


async def test_config_de_modelo_recusada_no_agente_sobe_como_invalid_model_config_error(impl: Implementation):
    with pytest.raises(InvalidModelConfigError):
        await impl.runtime.build_agent(impl.refused_agent)


async def test_config_de_modelo_recusada_no_team_sobe_como_invalid_model_config_error(impl: Implementation):
    agents = [await impl.runtime.build_agent(_agent("a1"))]

    with pytest.raises(InvalidModelConfigError):
        await asyncio.to_thread(impl.runtime.build_team, impl.refused_team, agents)
