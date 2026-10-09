"""``AgnoRuntime`` (F2-04): os handles são os objetos do agno que o AgentOS monta."""

from __future__ import annotations

import pytest
from agno.agent import Agent
from agno.team import Team

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.team_config import TeamConfig
from src.infrastructure.http.http_tool_factory import HttpToolFactory
from src.infrastructure.runtime.agno import AgnoRuntime, agent_factory_service, team_factory_service
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService
from src.infrastructure.runtime.agno.team_factory_service import TeamFactoryService
from tests.fakes import FakeEmbedderFactory, FakeModelFactory, InMemoryToolRepository, RecordingLogger

CONN = "mongodb://mongo.test.invalid:27017"


def _agent(agent_id: str) -> AgentConfig:
    return AgentConfig(id=agent_id, nome=agent_id, factory_ia_model="openai", model="m", descricao="d", prompt="p")


@pytest.fixture
def runtime(monkeypatch: pytest.MonkeyPatch) -> AgnoRuntime:
    monkeypatch.setattr(agent_factory_service, "MongoAgentDb", lambda **_: None)
    monkeypatch.setattr(team_factory_service, "MongoAgentDb", lambda **_: None)
    logger = RecordingLogger()
    models = FakeModelFactory()
    return AgnoRuntime(
        agent_factory=AgentFactoryService(
            db_url=CONN,
            logger=logger,
            model_factory=models,
            embedder_factory=FakeEmbedderFactory(),
            tool_factory=HttpToolFactory(logger=logger),
            tool_repository=InMemoryToolRepository(),
        ),
        team_factory=TeamFactoryService(db_url=CONN, logger=logger, model_factory=models),
    )


async def test_handles_sao_agent_e_team_do_agno_e_o_team_recebe_os_agentes_membros(runtime: AgnoRuntime):
    first = await runtime.build_agent(_agent("a1"))
    second = await runtime.build_agent(_agent("a2"))

    team = runtime.build_team(
        TeamConfig(id="t1", nome="T1", factory_ia_model="openai", model="m", member_ids=["a2"], mode="route"),
        [first, second],
    )

    assert isinstance(first, Agent) and isinstance(team, Team)
    assert team.members == [second]
