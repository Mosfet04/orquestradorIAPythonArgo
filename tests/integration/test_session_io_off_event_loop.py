"""F2-08/F3-09: nenhuma chamada do pymongo síncrono no event loop durante um run nem no shutdown.

Pilha de produção: fábricas reais, ``Agent``/``Team`` do agno e o db de sessões como o composition
root o cria (``create_session_db``: ``SessionDb`` com um ``AsyncMongoClient`` real, F3-09). O detector
(``trap_sync_pymongo``) intercepta a escolha de servidor do pymongo síncrono e a do cliente nativo, por
onde toda operação passa, registra e falha como um Mongo fora do ar, sem abrir socket.

Vermelho antes do F2-08: com um ``MongoDb`` síncrono por entidade, o mesmo run fazia
``createIndexes`` e ``find`` na thread do event loop, e o ``close`` de cada cliente também. Até o
F3-09 o cliente era o motor (pymongo síncrono numa thread do executor); agora nenhuma operação passa
pelo pymongo síncrono.
"""

from __future__ import annotations

import pytest
from agno.agent import Agent
from agno.run import RunStatus
from agno.team import Team
from pymongo.errors import ServerSelectionTimeoutError

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
    after_agent = len(pymongo_calls.async_operations)
    # Agno 3.x: a leitura da sessão do team não engole o erro (``team/_storage.py``,
    # ``_aread_session``): com o Mongo fora o run do team falha em vez de seguir com sessão nova.
    with pytest.raises(ServerSelectionTimeoutError):
        await team.arun("oi", user_id="ana", session_id="s-team")

    # Agno 3.x: com o Mongo fora o run do agente também falha (antes seguia sem sessão). O que importa
    # aqui é que o I/O de sessão aconteceu (índices e leitura, do agente e do team) e fora do loop.
    assert agent_run.status == RunStatus.error
    assert {"createIndexes", "find"} <= set(pymongo_calls.async_operations[:after_agent])
    assert len(pymongo_calls.async_operations) > after_agent >= 2  # o team também foi ao banco
    assert pymongo_calls.operations == [] and pymongo_calls.on_event_loop() == []
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
    assert "find" in pymongo_calls.async_operations
    assert pymongo_calls.operations == [] and pymongo_calls.on_event_loop() == []
    await db.close()


async def test_fechar_o_db_de_sessoes_aguarda_o_close_de_um_cliente_nativo(pymongo_calls: SyncPymongoCalls) -> None:
    db = create_session_db(DB_URL, "agno")
    agent = await _agent(db, "a1", memory=False)
    other = await _agent(db, "a2", memory=False)
    team = _team(db, agent)
    await agent.arun("oi", user_id="ana", session_id="s1")
    await other.arun("oi", user_id="ana", session_id="s2")
    with pytest.raises(ServerSelectionTimeoutError):  # agno 3.x: leitura de sessão do team não engole o erro
        await team.arun("oi", user_id="ana", session_id="s3")

    await db.close()
    await db.close()

    # Três entidades, um cliente; um close só, aguardado (antes do F2-08: um MongoClient.close por
    # entidade, no event loop; até o F3-09: o close síncrono do motor numa thread).
    assert (pymongo_calls.async_closes, pymongo_calls.closes) == (1, [])
