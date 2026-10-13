"""QA do F2-08: o detector ``trap_sync_pymongo`` pega de fato o I/O síncrono no event loop.

Sem estes casos, ``test_session_io_off_event_loop.py`` poderia passar mesmo com o detector cego (por
exemplo, depois de uma atualização do pymongo que mude o ponto de escolha de servidor). Aqui o erro
é plantado de propósito: chamada do pymongo síncrono feita no loop tem de aparecer em
``on_event_loop()``; a mesma chamada numa thread, não.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest
from agno.agent import Agent
from agno.db.base import SessionType
from agno.run import RunStatus
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo import MongoClient
from pymongo.errors import ServerSelectionTimeoutError

from src.infrastructure.runtime.agno.session_db import SessionDb
from tests.fakes import FakeChatModel
from tests.fakes.mongo import SyncPymongoCalls, trap_sync_pymongo

URL = "mongodb://mongo.test.invalid:27017"


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> SyncPymongoCalls:
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    return trap_sync_pymongo(monkeypatch)


def _blocking_find() -> None:
    """Uma consulta do pymongo síncrono, como o `MongoDb` antigo fazia dentro do `arun`."""
    client: MongoClient[dict[str, Any]] = MongoClient(URL, connect=False)
    try:
        with pytest.raises(ServerSelectionTimeoutError):  # o detector falha como um Mongo fora do ar
            client["agno"]["agno_sessions"].find_one({"session_id": "s1"})
    finally:
        client.close()


async def test_chamada_sincrona_no_event_loop_e_vista_e_a_mesma_numa_thread_nao(calls: SyncPymongoCalls) -> None:
    _blocking_find()
    assert [operation for operation, _ in calls.operations] == ["find"]
    assert calls.on_event_loop() == ["find"]  # bloqueou o loop

    calls.operations.clear()
    await asyncio.to_thread(_blocking_find)
    assert [operation for operation, _ in calls.operations] == ["find"]
    assert calls.on_event_loop() == []  # fora do loop: não é achado


async def test_close_do_cliente_sincrono_no_loop_e_registrado_como_tal(calls: SyncPymongoCalls) -> None:
    client: MongoClient[dict[str, Any]] = MongoClient(URL, connect=False)
    client.close()
    await asyncio.to_thread(MongoClient(URL, connect=False).close)

    assert calls.closes == [True, False]


async def test_operacao_do_motor_passa_pelo_detector_e_roda_fora_do_loop(calls: SyncPymongoCalls) -> None:
    client: AsyncIOMotorClient[dict[str, Any]] = AsyncIOMotorClient(URL)
    try:
        with pytest.raises(ServerSelectionTimeoutError):
            await client["agno"]["agno_sessions"].find_one({"session_id": "s1"})
    finally:
        await asyncio.to_thread(client.close)

    # O motor delega ao executor: o detector enxerga a operação e a thread sem loop.
    assert [operation for operation, _ in calls.operations] == ["find"]
    assert calls.on_event_loop() == []


class _BlockingSessionDb(SessionDb):
    """Regressão plantada: lê a sessão com o pymongo síncrono direto na corrotina (no event loop)."""

    async def get_session(self, session_id: str, session_type: SessionType, user_id: str | None = None,
                          deserialize: bool | None = True) -> Any:
        _blocking_find_sync()
        return None


def _blocking_find_sync() -> None:
    client: MongoClient[dict[str, Any]] = MongoClient(URL, connect=False)
    try:
        client["agno"]["agno_sessions"].find_one({"session_id": "s1"})
    except ServerSelectionTimeoutError:
        pass  # o db plantado ignora a falha, como o agno, que loga e segue
    finally:
        client.close()


async def test_run_com_db_que_bloqueia_o_loop_aparece_em_on_event_loop(calls: SyncPymongoCalls) -> None:
    db = _BlockingSessionDb(AsyncIOMotorClient(URL), db_url=URL, db_name="agno")
    agent = Agent(id="a", name="a", model=FakeChatModel(responses=["oi"] * 3), db=db, telemetry=False)

    run = await agent.arun("oi", user_id="ana", session_id="s1")
    await db.close()

    assert run.status == RunStatus.completed
    assert "find" in calls.on_event_loop()  # é isto que o teste de produção (sem a regressão) exige vazio


async def test_close_do_db_chamado_direto_no_loop_aparece_em_closes(calls: SyncPymongoCalls) -> None:
    """O mesmo para o shutdown: um ``close`` que não vá para a thread é registrado como ``True``."""

    class _CloseOnLoop(SessionDb):
        async def close(self) -> None:
            self._owned_client.close()  # regressão plantada: fecha no loop

    db = _CloseOnLoop(AsyncIOMotorClient(URL), db_url=URL, db_name="agno")
    await db.close()

    assert calls.closes == [True]
