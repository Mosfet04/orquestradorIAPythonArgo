"""F2-08: nenhuma chamada do pymongo síncrono no event loop durante um run nem no shutdown.

Pilha de produção: fábricas reais, ``Agent``/``Team`` do agno e o db de sessões como o composition
root o cria (``create_session_db``: ``SessionDb`` com um ``AsyncIOMotorClient`` real). O detector
(``trap_sync_pymongo``) intercepta a escolha de servidor do pymongo síncrono, por onde toda operação
passa, registra a thread e falha como um Mongo fora do ar, sem abrir socket.

Vermelho antes do F2-08: com um ``MongoDb`` síncrono por entidade, o mesmo run fazia
``createIndexes`` e ``find`` na thread do event loop, e o ``close`` de cada cliente também.
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
from src.infrastructure.runtime.agno.session_db import SessionDb, create_session_db
from src.infrastructure.runtime.agno.team_factory_service import TeamFactoryService
from tests.fakes import FakeEmbedderFactory, FakeModelFactory, InMemoryToolRepository, RecordingLogger
from tests.fakes.mongo import SyncPymongoCalls, trap_sync_pymongo

DB_URL = "mongodb://mongo.test.invalid:27017"


@pytest.fixture
def pymongo_calls(monkeypatch: pytest.MonkeyPatch) -> SyncPymongoCalls:
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    return trap_sync_pymongo(monkeypatch)


async def _agent(db: SessionDb, agent_id: str, *, memory: bool) -> Agent:
    logger = RecordingLogger()
    service = AgentFactoryService(
        db_url=DB_URL,
        session_db=db,
        logger=logger,
        model_factory=FakeModelFactory(responses=["resposta"] * 10),
        embedder_factory=FakeEmbedderFactory(),
        tool_factory=HttpToolFactory(logger=logger),
        tool_repository=InMemoryToolRepository(),
    )
    return await service.create_agent(
        AgentConfig(
            id=agent_id, nome=agent_id, factory_ia_model="fake", model="m", descricao="d", prompt="p",
            user_memory_active=memory, summary_active=True,
        )
    )


def _team(db: SessionDb, member: Agent) -> Team:
    service = TeamFactoryService(
        session_db=db, logger=RecordingLogger(), model_factory=FakeModelFactory(responses=["resposta do time"] * 10)
    )
    return service.create_team(
        TeamConfig(
            id="time", nome="time", factory_ia_model="fake", model="mt", member_ids=[member.id or ""],
            mode="coordinate", summary_active=True,
        ),
        [member],
    )


async def test_run_de_agente_e_de_team_nao_chama_o_pymongo_sincrono_no_event_loop(
    pymongo_calls: SyncPymongoCalls,
) -> None:
    db = create_session_db(DB_URL, "agno")
    agent = await _agent(db, "sem-memoria", memory=False)
    team = _team(db, agent)

    agent_run = await agent.arun("oi", user_id="ana", session_id="s-agente")
    after_agent = len(pymongo_calls.operations)
    await team.arun("oi", user_id="ana", session_id="s-team")

    # O run do agente termina mesmo com o Mongo fora (o agno loga a falha de sessão e segue), e o
    # I/O de sessão aconteceu: índices e leitura/gravação, do agente e do team, tudo fora do loop.
    assert (agent_run.status, agent_run.content) == (RunStatus.completed, "resposta")
    assert {"createIndexes", "find"} <= {operation for operation, _ in pymongo_calls.operations[:after_agent]}
    assert len(pymongo_calls.operations) > after_agent >= 2  # o team também foi ao banco
    assert pymongo_calls.on_event_loop() == []
    await db.close()


async def test_leitura_de_memoria_do_usuario_nao_chama_o_pymongo_sincrono_no_event_loop(
    pymongo_calls: SyncPymongoCalls,
) -> None:
    db = create_session_db(DB_URL, "agno")
    agent = await _agent(db, "com-memoria", memory=True)

    run = await agent.arun("o que você sabe de mim?", user_id="ana", session_id="s1")

    # Com o Mongo fora, ler a memória do usuário falha o run (como já era); o que importa aqui é
    # onde a leitura rodou.
    assert run.status == RunStatus.error
    assert pymongo_calls.operations and pymongo_calls.on_event_loop() == []
    await db.close()


async def test_fechar_o_db_de_sessoes_fecha_um_cliente_fora_do_event_loop(pymongo_calls: SyncPymongoCalls) -> None:
    db = create_session_db(DB_URL, "agno")
    agent = await _agent(db, "a1", memory=False)
    other = await _agent(db, "a2", memory=False)
    team = _team(db, agent)
    await agent.arun("oi", user_id="ana", session_id="s1")
    await other.arun("oi", user_id="ana", session_id="s2")
    await team.arun("oi", user_id="ana", session_id="s3")

    await db.close()
    await db.close()

    # Três entidades, um cliente; um close só, numa thread (antes: um MongoClient.close por
    # entidade, no event loop, cada um podendo esperar a seleção de servidor com o Mongo fora).
    assert pymongo_calls.closes == [False]
