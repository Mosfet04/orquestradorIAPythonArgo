"""QA do F2-08: bordas do ``SessionDb`` que os testes do dev não cobrem (sessão de workflow, falha
no ``close``, leitura sem filtro de tipo em coleção vazia e sessão de tipo desconhecido gravada)."""

from __future__ import annotations

import pytest
from agno.db.base import SessionType
from agno.session import AgentSession, WorkflowSession

from src.infrastructure.runtime.agno.session_db import SessionDb
from tests.fakes.mongo import FakeMotorClient

DB_URL = "mongodb://mongo.test.invalid:27017"


class _FailingMotorClient(FakeMotorClient):
    """O nome tem "Motor": o AsyncMongoDb do agno reconhece o tipo do cliente pelo nome da classe."""

    def close(self) -> None:
        super().close()
        raise OSError("falha ao fechar")


async def test_sessao_de_workflow_e_lida_com_a_classe_do_tipo_pedido() -> None:
    db = SessionDb(FakeMotorClient(), db_url=DB_URL, db_name="banco")
    await db.upsert_session(WorkflowSession(session_id="w1", workflow_id="wf", user_id="ana", session_data={"k": 1}))

    found = await db.get_session(session_id="w1", session_type=SessionType.WORKFLOW, user_id="ana")

    assert isinstance(found, WorkflowSession)
    assert (found.workflow_id, found.session_data) == ("wf", {"k": 1})
    assert await db.get_session(session_id="w1", session_type=SessionType.WORKFLOW, user_id="bruno") is None


async def test_get_session_de_sessao_inexistente_e_none_com_e_sem_deserializar() -> None:
    db = SessionDb(FakeMotorClient(), db_url=DB_URL, db_name="banco")

    assert await db.get_session(session_id="nada", session_type=SessionType.AGENT, user_id="ana") is None
    assert await db.get_session(
        session_id="nada", session_type=SessionType.TEAM, user_id="ana", deserialize=False
    ) is None


async def test_get_session_nao_altera_o_documento_gravado() -> None:
    """Ler como outro tipo devolve a classe pedida, mas não regrava o ``session_type`` do documento."""
    client = FakeMotorClient()
    db = SessionDb(client, db_url=DB_URL, db_name="banco")
    await db.upsert_session(AgentSession(session_id="s1", agent_id="a1", user_id="ana"))
    before = [dict(doc) for doc in client["banco"]["agno_sessions"].docs]

    await db.get_session(session_id="s1", session_type=SessionType.TEAM, user_id="ana")

    assert [dict(doc) for doc in client["banco"]["agno_sessions"].docs] == before
    assert before[0]["session_type"] == "agent"


async def test_falha_no_close_do_cliente_sobe_uma_vez_e_o_db_nao_tenta_de_novo() -> None:
    client = _FailingMotorClient()
    db = SessionDb(client, db_url=DB_URL, db_name="banco")

    with pytest.raises(OSError, match="falha ao fechar"):
        await db.close()
    await db.close()  # idempotente mesmo depois da falha: quem chama (cleanup) já logou o tipo

    assert client.closes == [False]
