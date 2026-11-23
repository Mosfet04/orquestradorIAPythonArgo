"""QA do F3-09 contra Mongo real (marker ``live``, ``MONGO_LIVE_URL``; nunca na CI padrão).

O ``close`` do ``AsyncMongoClient`` com conexão aberta (``endSessions`` async) e o uso depois dele.
"""

from __future__ import annotations

import asyncio

import pytest
from agno.db.schemas import UserMemory
from pymongo.errors import InvalidOperation

from src.infrastructure.runtime.agno.session_db import SessionDb
from tests.fakes.live_mongo import LiveMongo

pytestmark = [pytest.mark.live, pytest.mark.usefixtures("live_native_closed")]


async def test_close_com_conexao_aberta_e_idempotente_e_o_uso_depois_dele_e_recusado(live_mongo: LiveMongo) -> None:
    db = SessionDb(live_mongo.native(), db_url="mongodb://mongo.live.invalid:27017", db_name=live_mongo.database)
    await db.upsert_user_memory(UserMemory(memory="Ana prefere espanhol", user_id="ana"))
    assert [m.memory for m in await db.get_user_memories(user_id="ana")] == ["Ana prefere espanhol"]  # type: ignore[union-attr]

    await asyncio.wait_for(asyncio.gather(db.close(), db.close()), timeout=10)
    await db.close()

    with pytest.raises(InvalidOperation) as caught:
        await db.get_user_memories(user_id="ana")
    leaked = live_mongo.url in str(caught.value)
    assert not leaked, "a URI do Mongo vazou na mensagem"
    # o dado ficou gravado (lido por outro caminho, o pymongo síncrono do fixture)
    assert live_mongo.collection("agno_memories").count_documents({"user_id": "ana"}) == 1
