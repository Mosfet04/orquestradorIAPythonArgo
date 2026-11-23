"""QA do F3-09: o ``SessionDb`` sobre o ``AsyncMongoClient`` nativo do pymongo.

Cobre o que os testes do dev não fixam: (R1) o agno reconhece os fakes como cliente nativo só pelo nome
da classe, então uma mudança na heurística dele tem de quebrar um teste com nome claro; (R4) o teardown
da fixture ``live_native_closed``; o cliente real (sem rede) criado fora de loop, usado depois do
``close`` e com o Mongo fora, sem vazar a URI.
"""

from __future__ import annotations

import asyncio
import warnings
from typing import Any

import pytest
from agno.db.mongo import AsyncMongoDb
from agno.db.mongo.async_mongo import _detect_client_type
from pymongo import AsyncMongoClient
from pymongo.errors import InvalidOperation, ServerSelectionTimeoutError

from src.infrastructure.runtime.agno.session_db import SessionDb, create_session_db
from tests.fakes import startup_world
from tests.fakes.live_mongo import LiveMongo
from tests.fakes.mongo import FakeAsyncMongoClient, FakeMotorClient, SyncPymongoCalls, trap_sync_pymongo

pytestmark = pytest.mark.unit

MARKER = "SENHA-DO-MONGO-F309"
MONGO_URL_WITH_MARKER = "mongodb://" + "usuario:" + MARKER + "@mongo.test.invalid:27017"


@pytest.fixture
def calls(monkeypatch: pytest.MonkeyPatch) -> SyncPymongoCalls:
    return trap_sync_pymongo(monkeypatch)


# --- R1: a detecção do agno como contrato do fake -------------------------------------------------


class _SubclasseAsyncMongoComNomeCerto(FakeAsyncMongoClient):
    pass


def test_fake_do_cliente_nativo_e_detectado_pelo_agno_como_pymongo_async() -> None:
    """Se o agno mudar a heurística (hoje: nome da classe), este teste quebra, e não os de comportamento."""
    assert _detect_client_type(FakeAsyncMongoClient()) == AsyncMongoDb.CLIENT_TYPE_PYMONGO_ASYNC
    real = AsyncMongoClient("mongodb://mongo.test.invalid")  # o real é reconhecido por isinstance
    assert _detect_client_type(real) == "pymongo_async"
    # E o db de sessões, sobre o fake, escolhe o caminho do cliente nativo.
    db = SessionDb(FakeAsyncMongoClient(), db_url="mongodb://x.invalid", db_name="b")  # type: ignore[arg-type]
    assert db._client_type == AsyncMongoDb.CLIENT_TYPE_PYMONGO_ASYNC


@pytest.mark.parametrize(
    "client_type",
    [_SubclasseAsyncMongoComNomeCerto, startup_world._SessionAsyncMongoClient],
    ids=lambda cls: cls.__name__,
)
def test_subclasses_do_fake_usadas_nos_testes_sao_detectadas_como_nativas(
    client_type: type[FakeAsyncMongoClient],
) -> None:
    """Lista explícita (independe de quais módulos o worker do xdist já carregou): o que importa é o nome."""
    kwargs: dict[str, Any] = (
        {"events": startup_world.Events()} if client_type is startup_world._SessionAsyncMongoClient else {}
    )

    assert _detect_client_type(client_type(**kwargs)) == AsyncMongoDb.CLIENT_TYPE_PYMONGO_ASYNC


def test_o_nome_da_classe_e_o_que_decide_o_tipo_o_fake_do_motor_nao_e_nativo() -> None:
    assert _detect_client_type(FakeMotorClient()) == AsyncMongoDb.CLIENT_TYPE_MOTOR


def test_fake_sem_o_token_no_nome_nao_e_aceito_pelo_agno() -> None:
    """Documenta por que o nome importa: sem ``AsyncMongo``/``Motor`` o agno recusa o cliente."""

    class ClienteFalso(FakeAsyncMongoClient):
        pass

    with pytest.raises(ValueError, match="Unsupported MongoDB client type"):
        SessionDb(ClienteFalso(), db_url="mongodb://x.invalid", db_name="b")  # type: ignore[arg-type]


# --- R4: teardown robusto da fixture live_native_closed -------------------------------------------


class _FakeNative:
    def __init__(self, error: BaseException | None = None) -> None:
        self.error = error
        self.closed = 0

    async def close(self) -> None:
        self.closed += 1
        if self.error is not None:
            raise self.error


def _live_with(natives: list[_FakeNative]) -> LiveMongo:
    live = LiveMongo(url="mongodb://nunca-conecta.invalid", database="orq_live_teste")
    live._natives = natives  # type: ignore[assignment]
    return live


async def test_close_natives_fecha_todos_e_esvazia_a_lista() -> None:
    natives = [_FakeNative(), _FakeNative()]
    live = _live_with(natives)

    await live.close_natives()
    await live.close_natives()  # idempotente: a lista foi limpa

    assert [n.closed for n in natives] == [1, 1]
    assert live._natives == []


async def test_close_natives_com_um_close_que_falha_fecha_os_outros_limpa_a_lista_e_sobe_o_erro() -> None:
    natives = [_FakeNative(), _FakeNative(OSError("falha ao fechar")), _FakeNative()]
    live = _live_with(natives)

    with pytest.raises(OSError, match="falha ao fechar"):
        await live.close_natives()

    assert [n.closed for n in natives] == [1, 1, 1]  # o erro do meio não deixou o último aberto
    assert live._natives == []  # e não fica estado para o teste seguinte


async def test_close_natives_com_varios_closes_que_falham_sobe_todos_os_erros_num_grupo() -> None:
    natives = [_FakeNative(OSError("um")), _FakeNative(), _FakeNative(RuntimeError("dois"))]
    live = _live_with(natives)

    with pytest.raises(BaseExceptionGroup) as caught:
        await live.close_natives()

    assert sorted(type(e).__name__ for e in caught.value.exceptions) == ["OSError", "RuntimeError"]
    assert [n.closed for n in natives] == [1, 1, 1]
    assert live._natives == []


# --- cliente real, sem rede -----------------------------------------------------------------------


@pytest.mark.filterwarnings("error::pytest.PytestUnraisableExceptionWarning")
def test_create_session_db_fora_de_um_event_loop_nao_conecta_e_fecha_no_loop_de_quem_usa() -> None:
    """O cliente só se prende ao loop da primeira operação: criar fora de loop é seguro (CLI, container)."""
    with warnings.catch_warnings():
        warnings.simplefilter("error")  # avisos de código síncrono viram falha (os do GC: filterwarnings acima)
        db = create_session_db(MONGO_URL_WITH_MARKER, "banco")
        assert isinstance(db.db_client, AsyncMongoClient)
        assert db.db_client._opened is False

        asyncio.run(db.close())
        asyncio.run(db.close())  # segunda chamada, em outro loop: não faz nada


async def test_uso_depois_do_close_com_o_cliente_real_levanta_invalid_operation_sem_a_uri() -> None:
    """Sem o detector: a recusa do driver vem antes de qualquer escolha de servidor (nenhuma rede)."""
    db = create_session_db(MONGO_URL_WITH_MARKER, "banco")
    await db.close()

    with pytest.raises(InvalidOperation) as caught:
        await db.get_user_memories(user_id="ana")

    assert MARKER not in str(caught.value) and "mongo.test.invalid" not in str(caught.value)


async def test_com_o_mongo_fora_a_operacao_falha_o_close_roda_uma_vez_e_nada_vai_ao_pymongo_sincrono(
    calls: SyncPymongoCalls,
) -> None:
    db = create_session_db(MONGO_URL_WITH_MARKER, "banco")

    with pytest.raises(ServerSelectionTimeoutError):
        await db.get_user_memories(user_id="ana")
    await asyncio.gather(db.close(), db.close())

    # a leitura passou pelo cliente nativo (o agno cria o índice antes; nenhuma escrita de dado)
    assert "count" in calls.async_operations
    assert set(calls.async_operations) <= {"createIndexes", "count"}
    assert calls.operations == [] and calls.closes == []  # e não pelo pymongo síncrono do motor
    assert calls.async_closes == 1


async def test_close_concorrente_com_o_cliente_fake_fecha_uma_vez_mesmo_com_close_lento() -> None:
    """Dois ``close`` em voo enquanto o primeiro ainda espera o driver: o cliente fecha uma vez só."""
    gate = asyncio.Event()

    class SlowAsyncMongoClient(FakeAsyncMongoClient):
        async def close(self) -> None:
            await gate.wait()
            await super().close()

    client = SlowAsyncMongoClient()
    db = SessionDb(client, db_url="mongodb://x.invalid", db_name="b")  # type: ignore[arg-type]

    first = asyncio.create_task(db.close())
    second = asyncio.create_task(db.close())
    await asyncio.sleep(0)
    gate.set()
    await asyncio.gather(first, second)

    assert client.closes == [True]
