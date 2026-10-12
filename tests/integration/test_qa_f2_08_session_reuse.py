"""QA do F2-08: reuso de ``session_id`` entre agente e team, nos dois sentidos, em run de verdade.

Decisão (1) do F2-08: como com o ``MongoDb`` síncrono, quem recebe o ``session_id`` de uma sessão de
outro tipo continua a conversa. O dev cobriu agente -> team -> agente (``test_session_db_wiring.py``) e
a leitura no nível do db; aqui entram team -> agente, a conferência do que o modelo recebe (o histórico
chega de fato ao contexto) e as sessões de um tipo só. ``add_history_to_context`` vem das fábricas.
"""

from __future__ import annotations

import pytest
from agno.agent import Agent
from agno.run import RunStatus
from agno.team import Team

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.team_config import TeamConfig
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from src.infrastructure.runtime.agno.session_db import SessionDb
from src.infrastructure.runtime.agno.team_factory_service import TeamFactoryService
from tests.fakes import (
    FakeChatModel,
    FakeEmbedderFactory,
    FakeModelFactory,
    InMemoryToolRepository,
    RecordingLogger,
)
from tests.fakes.session_db import fake_session_db, session_docs

FIRST = "meu código de cliente é 4815"
SECOND = "qual é o meu código?"


@pytest.fixture(autouse=True)
def _no_agno_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGNO_TELEMETRY", "false")


async def _agent(db: SessionDb, agent_id: str = "ag") -> Agent:
    logger = RecordingLogger()
    service = AgentFactoryService(
        db_url="mongodb://mongo.test.invalid:27017",
        session_db=db,
        logger=logger,
        model_factory=FakeModelFactory(responses=["resposta"] * 10),
        embedder_factory=FakeEmbedderFactory(),
        tool_factory=HttpToolFactory(logger=logger),
        tool_repository=InMemoryToolRepository(),
    )
    return await service.create_agent(
        AgentConfig(id=agent_id, nome=agent_id, factory_ia_model="fake", model="m", descricao="d", prompt="p")
    )


def _team(db: SessionDb, member: Agent) -> Team:
    service = TeamFactoryService(
        session_db=db, logger=RecordingLogger(), model_factory=FakeModelFactory(responses=["do time"] * 10)
    )
    return service.create_team(
        TeamConfig(id="time", nome="time", factory_ia_model="fake", model="mt", member_ids=[member.id or ""],
                   mode="coordinate"),
        [member],
    )


def _prompt(entity: Agent | Team) -> str:
    model = entity.model
    assert isinstance(model, FakeChatModel)
    return "\n".join(content for call in model.calls for _, content in call.messages)


async def test_agente_que_recebe_o_session_id_de_um_team_ve_o_historico_dele() -> None:
    db = fake_session_db()
    member = await _agent(db, "membro")
    team = _team(db, member)
    standalone = await _agent(db, "avulso")

    first = await team.arun(FIRST, user_id="ana", session_id="s1")
    second = await standalone.arun(SECOND, user_id="ana", session_id="s1")

    assert (first.status, second.status) == (RunStatus.completed, RunStatus.completed)
    assert FIRST in _prompt(standalone)  # o histórico do team chegou ao modelo do agente
    docs = session_docs(db)
    assert [(d["session_id"], d["user_id"], d["session_type"]) for d in docs] == [("s1", "ana", "agent")]
    # Um documento só, com as duas execuções. O tipo é o de quem gravou por último (o ``upsert_session``
    # substitui o documento inteiro, também no ``MongoDb`` síncrono): o histórico não se perde.
    assert len(docs[0]["runs"]) == 2


async def test_team_que_recebe_o_session_id_de_um_agente_ve_o_historico_dele() -> None:
    db = fake_session_db()
    owner = await _agent(db, "dono")
    member = await _agent(db, "membro")
    team = _team(db, member)

    await owner.arun(FIRST, user_id="ana", session_id="s1")
    second = await team.arun(SECOND, user_id="ana", session_id="s1")

    assert second.status == RunStatus.completed
    assert FIRST in _prompt(team)
    docs = session_docs(db)
    assert [(d["session_id"], d["user_id"], d["session_type"]) for d in docs] == [("s1", "ana", "team")]
    assert len(docs[0]["runs"]) == 2


async def test_sessoes_de_um_tipo_so_continuam_separadas_por_session_id() -> None:
    db = fake_session_db()
    member = await _agent(db, "membro")
    team = _team(db, member)
    other = await _agent(db, "outro")

    await other.arun(FIRST, user_id="ana", session_id="s-agente")
    await team.arun("conversa do time", user_id="ana", session_id="s-team")
    await other.arun(SECOND, user_id="ana", session_id="s-agente")

    assert FIRST in _prompt(other)  # o mesmo agente, mesma sessão: histórico
    assert "conversa do time" not in _prompt(other)  # sessões diferentes não se misturam
    assert sorted((d["session_id"], d["session_type"], len(d["runs"])) for d in session_docs(db)) == [
        ("s-agente", "agent", 2),
        ("s-team", "team", 1),
    ]
