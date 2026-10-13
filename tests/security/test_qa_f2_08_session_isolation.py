"""QA do F2-08: isolamento entre usuários no ``SessionDb`` com o ``get_session`` sem filtro de tipo.

A decisão (1) do F2-08 tira ``session_type`` da consulta para o histórico seguir quando outro tipo de
entidade recebe o ``session_id``. Isso não pode abrir a sessão de um usuário para outro: o filtro por
``user_id`` continua na consulta, em qualquer combinação de tipos. Roda com o ``AsyncMongoDb`` real do
agno sobre o cliente motor em memória; modelos são o ``FakeChatModel``.
"""

from __future__ import annotations

import copy

import pytest
from agno.agent import Agent
from agno.db.base import SessionType
from agno.run import RunStatus
from agno.session import AgentSession, TeamSession
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

HISTORY_MARK = "o cofre da Ana abre com 7421"


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


@pytest.mark.parametrize(
    ("stored", "asked_as"),
    [
        (AgentSession(session_id="s1", agent_id="a1", user_id="ana"), SessionType.AGENT),
        (AgentSession(session_id="s1", agent_id="a1", user_id="ana"), SessionType.TEAM),
        (TeamSession(session_id="s1", team_id="t1", user_id="ana"), SessionType.AGENT),
        (TeamSession(session_id="s1", team_id="t1", user_id="ana"), SessionType.TEAM),
    ],
    ids=["agente-lido-como-agente", "agente-lido-como-team", "team-lido-como-agente", "team-lido-como-team"],
)
async def test_outro_usuario_nao_le_a_sessao_pelo_session_id_em_nenhuma_combinacao_de_tipos(
    stored: AgentSession | TeamSession, asked_as: SessionType
) -> None:
    db = fake_session_db()
    await db.upsert_session(stored)

    for deserialize in (True, False):
        assert await db.get_session(
            session_id="s1", session_type=asked_as, user_id="bruno", deserialize=deserialize
        ) is None
    assert await db.get_session(session_id="s1", session_type=asked_as, user_id="ana") is not None


@pytest.mark.parametrize("other_is_team", [False, True], ids=["bruno-no-agente", "bruno-no-team"])
async def test_run_de_outro_usuario_com_o_session_id_de_ana_nao_ve_nem_toca_o_historico_dela(
    other_is_team: bool,
) -> None:
    """Ana conversa num agente; Bruno manda o mesmo ``session_id`` a um agente ou a um team."""
    db = fake_session_db()
    owner = await _agent(db)
    await owner.arun(HISTORY_MARK, user_id="ana", session_id="s-ana")
    before = copy.deepcopy(session_docs(db))
    assert [(d["user_id"], d["session_type"]) for d in before] == [("ana", "agent")]

    if other_is_team:
        member = await _agent(db, "membro")
        entity: Agent | Team = _team(db, member)
    else:
        entity = await _agent(db, "outro")
    result = await entity.arun("qual é o código do cofre?", user_id="bruno", session_id="s-ana")

    assert result.status == RunStatus.completed
    assert HISTORY_MARK not in _prompt(entity)  # o histórico da Ana não entra no contexto do Bruno
    anas = [d for d in session_docs(db) if d["user_id"] == "ana"]
    assert len(anas) == 1 and anas[0]["session_id"] == "s-ana"
    assert len(anas[0]["runs"]) == 1  # nenhuma execução do Bruno foi gravada na sessão da Ana
    assert HISTORY_MARK in str(anas[0]["runs"][0]["input"])
    assert anas == before  # o documento dela segue como estava


async def test_upsert_de_outro_usuario_e_de_outro_tipo_sobre_a_sessao_de_ana_e_recusado() -> None:
    db = fake_session_db()
    await db.upsert_session(AgentSession(session_id="s1", agent_id="a1", user_id="ana", session_data={"de": "ana"}))

    refused = await db.upsert_session(
        TeamSession(session_id="s1", team_id="t1", user_id="bruno", session_data={"de": "bruno"})
    )

    assert refused is None
    assert [(d["user_id"], d["session_type"], d["session_data"]) for d in session_docs(db)] == [
        ("ana", "agent", {"de": "ana"})
    ]
