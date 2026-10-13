"""F2-08: ``SessionDb``, o db único de sessões e memória (``AsyncMongoDb`` do agno 2.5.8 sobre um
cliente motor só).

O cliente aqui é o ``FakeMotorClient`` (sem Mongo); o que roda é o ``AsyncMongoDb`` real do agno,
com as consultas e os documentos que ele monta. Contra Mongo de verdade: ``tests/integration/
test_session_db_live.py`` (marker ``live``).
"""

from __future__ import annotations

import asyncio

import pytest
from agno.db.base import AsyncBaseDb, BaseDb, SessionType
from agno.db.schemas import UserMemory
from agno.session import AgentSession, TeamSession
from agno.utils.string import generate_id
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import InvalidOperation

from src.infrastructure.runtime.agno.session_db import SessionDb, create_session_db
from tests.fakes.mongo import FakeMotorClient

DB_URL = "mongodb://mongo.test.invalid:27017"
DB_NAME = "banco_de_teste"


def _db(client: FakeMotorClient | None = None) -> SessionDb:
    return SessionDb(client or FakeMotorClient(), db_url=DB_URL, db_name=DB_NAME)


def test_e_um_db_assincrono_do_agno_com_as_colecoes_e_o_id_do_mongodb_sincrono() -> None:
    db = _db()

    # AsyncBaseDb e não BaseDb: Agent/Team fazem o I/O com await e o AgentOS não carrega
    # agente/team do banco (``agno/os/utils.py``, ``isinstance(db, BaseDb)``).
    assert isinstance(db, AsyncBaseDb) and not isinstance(db, BaseDb)
    assert (db.db_name, db.session_table_name, db.memory_table_name) == (DB_NAME, "agno_sessions", "agno_memories")
    # Mesmo id que o ``MongoDb`` síncrono derivava da URL e do banco: ``db_id`` das rotas não muda.
    assert db.id == generate_id(f"{DB_URL}#{DB_NAME}")


async def test_create_session_db_usa_um_cliente_motor_que_so_conecta_no_primeiro_uso() -> None:
    db = create_session_db(DB_URL, DB_NAME)

    assert isinstance(db, SessionDb)
    assert isinstance(db.db_client, AsyncIOMotorClient)
    assert db.db_client is db.db_client  # um cliente só, o mesmo a cada acesso
    assert (db.db_name, db.id) == (DB_NAME, generate_id(f"{DB_URL}#{DB_NAME}"))
    # connect=False do motor: nenhuma thread de monitor nem socket até a primeira operação.
    assert db.db_client.delegate._opened is False
    await db.close()


async def test_sessao_e_gravada_e_lida_por_user_id() -> None:
    client = FakeMotorClient()
    db = _db(client)

    await db.upsert_session(AgentSession(session_id="s1", agent_id="a1", user_id="ana", session_data={"k": "v"}))

    stored = client[DB_NAME]["agno_sessions"].docs
    assert [(d["session_id"], d["session_type"], d["agent_id"], d["user_id"]) for d in stored] == [
        ("s1", "agent", "a1", "ana")
    ]
    mine = await db.get_session(session_id="s1", session_type=SessionType.AGENT, user_id="ana")
    assert isinstance(mine, AgentSession) and mine.session_data == {"k": "v"}
    assert await db.get_session(session_id="s1", session_type=SessionType.AGENT, user_id="bruno") is None


async def test_sessao_de_um_usuario_nao_e_sobrescrita_por_outro() -> None:
    client = FakeMotorClient()
    db = _db(client)
    await db.upsert_session(AgentSession(session_id="s1", agent_id="a1", user_id="ana", session_data={"de": "ana"}))

    refused = await db.upsert_session(
        AgentSession(session_id="s1", agent_id="a1", user_id="bruno", session_data={"de": "bruno"})
    )

    assert refused is None
    assert [(d["user_id"], d["session_data"]) for d in client[DB_NAME]["agno_sessions"].docs] == [
        ("ana", {"de": "ana"})
    ]


# ── get_session sem filtro por tipo, como o MongoDb síncrono (decisão do F2-08) ──


async def test_get_session_consulta_por_session_id_e_usuario_sem_filtrar_por_tipo() -> None:
    """O ``AsyncMongoDb`` do agno 2.5.8 poria ``session_type`` na consulta; o ``MongoDb`` síncrono não."""
    client = FakeMotorClient()
    db = _db(client)
    await db.upsert_session(AgentSession(session_id="s1", agent_id="a1", user_id="ana"))
    sessions = client[DB_NAME]["agno_sessions"]
    sessions.queries.clear()

    await db.get_session(session_id="s1", session_type=SessionType.TEAM, user_id="ana")
    await db.get_session(session_id="s1", session_type=SessionType.AGENT)

    assert sessions.queries == [{"session_id": "s1", "user_id": "ana"}, {"session_id": "s1"}]


@pytest.mark.parametrize(
    ("first", "then_type", "then_class"),
    [
        (AgentSession(session_id="s1", agent_id="a1", user_id="ana", session_data={"historico": "anterior"}),
         SessionType.TEAM, TeamSession),
        (TeamSession(session_id="s1", team_id="t1", user_id="ana", session_data={"historico": "anterior"}),
         SessionType.AGENT, AgentSession),
    ],
    ids=["agente-depois-team", "team-depois-agente"],
)
async def test_outro_tipo_de_entidade_que_reusa_o_session_id_mantem_o_historico_como_no_mongodb_sincrono(
    first: AgentSession | TeamSession, then_type: SessionType, then_class: type
) -> None:
    client = FakeMotorClient()
    db = _db(client)
    await db.upsert_session(first)

    found = await db.get_session(session_id="s1", session_type=then_type, user_id="ana")
    assert isinstance(found, then_class)  # a classe é a do tipo pedido; os dados, os do documento
    assert found.session_data == {"historico": "anterior"}
    await db.upsert_session(found)

    docs = client[DB_NAME]["agno_sessions"].docs
    assert [(d["session_id"], d["session_type"], d["session_data"]) for d in docs] == [
        ("s1", then_type.value, {"historico": "anterior"})
    ]


async def test_sessao_de_um_tipo_so_e_lida_como_antes() -> None:
    db = _db()
    await db.upsert_session(AgentSession(session_id="s-agente", agent_id="a1", user_id="ana", session_data={"k": 1}))
    await db.upsert_session(TeamSession(session_id="s-team", team_id="t1", user_id="ana", session_data={"k": 2}))

    agent_session = await db.get_session(session_id="s-agente", session_type=SessionType.AGENT, user_id="ana")
    team_session = await db.get_session(session_id="s-team", session_type=SessionType.TEAM, user_id="ana")
    raw = await db.get_session(
        session_id="s-team", session_type=SessionType.TEAM, user_id="ana", deserialize=False
    )

    assert isinstance(agent_session, AgentSession)
    assert (agent_session.agent_id, agent_session.session_data) == ("a1", {"k": 1})
    assert isinstance(team_session, TeamSession)
    assert (team_session.team_id, team_session.session_data) == ("t1", {"k": 2})
    assert isinstance(raw, dict) and (raw["session_type"], raw["team_id"]) == ("team", "t1")
    # O filtro por usuário continua: sessão de outro usuário ou inexistente não é devolvida.
    assert await db.get_session(session_id="s-agente", session_type=SessionType.AGENT, user_id="bruno") is None
    assert await db.get_session(session_id="nao-existe", session_type=SessionType.AGENT) is None


async def test_get_session_com_tipo_desconhecido_levanta_como_no_mongodb_sincrono() -> None:
    db = _db()
    await db.upsert_session(AgentSession(session_id="s1", agent_id="a1", user_id="ana"))

    with pytest.raises(ValueError, match="Invalid session type"):
        await db.get_session(session_id="s1", session_type="outro", user_id="ana")  # type: ignore[arg-type]
    raw = await db.get_session(
        session_id="s1", session_type="outro", user_id="ana", deserialize=False  # type: ignore[arg-type]
    )
    assert isinstance(raw, dict) and raw["session_id"] == "s1"


async def test_memoria_e_gravada_e_lida_por_user_id() -> None:
    client = FakeMotorClient()
    db = _db(client)

    await db.upsert_user_memory(UserMemory(memory="Ana prefere espanhol", user_id="ana", topics=["idioma"]))
    await db.upsert_user_memory(UserMemory(memory="Bruno é alérgico a amendoim", user_id="bruno"))

    ana = await db.get_user_memories(user_id="ana")
    assert [(m.user_id, m.memory, m.topics) for m in ana] == [("ana", "Ana prefere espanhol", ["idioma"])]
    assert sorted(d["user_id"] for d in client[DB_NAME]["agno_memories"].docs) == ["ana", "bruno"]


async def test_close_fecha_o_cliente_uma_vez_e_fora_do_event_loop() -> None:
    client = FakeMotorClient()
    db = _db(client)
    await db.upsert_user_memory(UserMemory(memory="m", user_id="ana"))

    await db.close()
    await db.close()
    await asyncio.gather(db.close(), db.close())

    # Uma chamada só, numa thread sem event loop: o close do motor é o do pymongo síncrono
    # (endSessions pela rede) e, no loop, travaria o shutdown com o Mongo fora do ar.
    assert client.closes == [False]


async def test_uso_depois_do_close_falha_alto() -> None:
    db = _db()
    await db.close()

    with pytest.raises(InvalidOperation):
        await db.get_user_memories(user_id="ana")
