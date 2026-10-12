"""Db único de sessões e memória das entidades e do AgentOS (F2-08).

Um ``AsyncMongoDb`` do agno 2.5 para todos os agentes e teams, no lugar de um ``MongoDb``
síncrono por entidade: o I/O de sessão e memória sai do event loop e há um cliente só para
fechar. Mesmas coleções (``agno_sessions``, ``agno_memories``...), mesmos documentos e mesmos
índices do ``MongoDb`` síncrono (``agno/db/mongo/schemas.py`` vale para os dois).

Conferido no agno 2.5.8 com pymongo 4.18 e motor 3.7 (``agno/db/mongo/async_mongo.py``):

- o cliente é o **motor**. O ``AsyncMongoClient`` do pymongo não serve nesta versão do agno: a
  detecção dele falha no import (``PYMONGO_ASYNC_AVAILABLE`` fica ``False``), ``aggregate(...)
  .to_list`` é escrito para o motor e o ``close`` não é aguardado. O motor roda cada chamada do
  pymongo numa thread do executor;
- o ``close`` do agno chama ``client.close()`` no event loop, e o do motor é o do pymongo
  síncrono (manda ``endSessions`` pela rede; com o Mongo fora espera a seleção de servidor). Por
  isso o cliente é criado aqui e fechado aqui, numa thread;
- o ``get_session`` do ``AsyncMongoDb`` filtra por ``session_type`` e o do ``MongoDb`` não: aqui
  a leitura é a do síncrono (ver ``SessionDb.get_session``).
"""

from __future__ import annotations

import asyncio
from typing import Any

from agno.db.base import SessionType
from agno.db.mongo import AsyncMongoDb
from agno.session import AgentSession, Session, TeamSession, WorkflowSession
from motor.motor_asyncio import AsyncIOMotorClient


class SessionDb(AsyncMongoDb):
    """``AsyncMongoDb`` do agno sobre um cliente motor que é dele: ``close`` fecha esse cliente.

    ``db_url`` só entra na semente do ``id`` do db (o mesmo que o ``MongoDb`` síncrono derivava de
    URL e banco: o ``db_id`` das rotas do AgentOS não muda); quem conecta é o ``client``.
    """

    def __init__(self, client: AsyncIOMotorClient[dict[str, Any]], *, db_url: str, db_name: str) -> None:
        super().__init__(db_client=client, db_url=db_url, db_name=db_name)
        self._owned_client = client
        self._closed = False

    async def get_session(
        self,
        session_id: str,
        session_type: SessionType,
        user_id: str | None = None,
        deserialize: bool | None = True,
    ) -> Session | dict[str, Any] | None:
        """Lê a sessão por ``session_id`` (e ``user_id``) sem filtrar por ``session_type``.

        Como o ``MongoDb`` síncrono (``agno/db/mongo/mongo.py``, ``get_session``).

        O ``AsyncMongoDb`` do agno 2.5.8 filtra também pelo tipo. Com isso um team que recebe o
        ``session_id`` de uma sessão de agente (ou o contrário) não a acharia, criaria outra e o
        ``upsert_session`` trocaria o documento, perdendo o histórico. Decisão do F2-08: manter o
        comportamento de antes (a conversa continua). ``session_type`` só escolhe a classe da
        sessão devolvida. Rever no upgrade para o agno 3.x (F3).
        """
        # Sem tipo, o agno não põe o filtro (``if session_type is not None``) e devolve o documento.
        document = await super().get_session(
            session_id=session_id,
            session_type=None,  # type: ignore[arg-type]
            user_id=user_id,
            deserialize=False,
        )
        if not deserialize or not isinstance(document, dict):
            return document
        if session_type == SessionType.AGENT:
            return AgentSession.from_dict(document)
        if session_type == SessionType.TEAM:
            return TeamSession.from_dict(document)
        if session_type == SessionType.WORKFLOW:
            return WorkflowSession.from_dict(document)
        raise ValueError(f"Invalid session type: {session_type}")

    async def close(self) -> None:
        """Fecha o cliente uma vez e fora do event loop; chamadas seguintes não fazem nada.

        Quem chama: o lifespan de db do AgentOS (``_close_databases``) e o ``cleanup`` do
        container (que vale também quando o AgentOS não chegou a subir). Depois disso o cliente
        recusa o uso (``InvalidOperation`` do pymongo).
        """
        if self._closed:
            return
        self._closed = True
        await asyncio.to_thread(self._owned_client.close)


def create_session_db(db_url: str, db_name: str) -> SessionDb:
    """Db de sessões do app, com um cliente motor novo (só conecta na primeira operação).

    Opções do cliente (timeouts, pool) são as do driver e as da própria URI, como no ``MongoDb``
    síncrono que este db substitui.
    """
    return SessionDb(AsyncIOMotorClient(db_url), db_url=db_url, db_name=db_name)
