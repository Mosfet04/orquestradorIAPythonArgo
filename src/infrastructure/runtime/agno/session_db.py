"""Db único de sessões e memória das entidades e do AgentOS (F2-08).

Um ``AsyncMongoDb`` do agno para todos os agentes e teams, no lugar de um ``MongoDb``
síncrono por entidade: o I/O de sessão e memória sai do event loop e há um cliente só para
fechar. Mesmas coleções (``agno_sessions``, ``agno_memories``...), mesmos documentos e mesmos
índices do ``MongoDb`` síncrono (``agno/db/mongo/schemas.py`` vale para os dois).

Conferido no agno 3.1.12 com pymongo 4.18 e motor 3.7 (``agno/db/mongo/async_mongo.py``):

- o cliente é o **motor** (o agno detecta o tipo pelo módulo do cliente). O motor roda cada
  chamada do pymongo numa thread do executor;
- o ``close`` do agno chama ``client.close()`` no event loop, e o do motor é o do pymongo
  síncrono (manda ``endSessions`` pela rede; com o Mongo fora espera a seleção de servidor). Por
  isso o cliente é criado aqui e fechado aqui, numa thread;
- o ``get_session`` do agno 3.x lê por ``session_id`` (e ``user_id``) sem filtrar por
  ``session_type`` (o tipo só escolhe a classe devolvida) e junta os runs da coleção
  ``agno_runs`` aos do campo legado ``runs``: a conversa continua quando um team recebe o
  ``session_id`` de uma sessão de agente (decisão do F2-08), sem override aqui;
- o ``upsert_run`` do agno 3.x grava em ``agno_runs`` com ``replace_one({"run_id": ...})``, sem
  sessão nem dono no filtro, e o ``run_id`` pode vir do cliente (form da rota REST, ``runId`` do
  AG-UI): o ``run_id`` de um run de outra sessão trocaria o documento dele, e um run com o
  ``session_id`` de outro usuário entraria no histórico dele (o ``upsert_session`` recusa, o
  ``upsert_run`` não). Aqui as duas gravações são recusadas (ver ``upsert_run``).
"""

from __future__ import annotations

import asyncio
from typing import Any

from agno.db.mongo import AsyncMongoDb
from agno.db.utils import build_single_run_row
from agno.run.agent import RunOutput
from agno.run.team import TeamRunOutput
from agno.run.workflow import WorkflowRunOutput
from motor.motor_asyncio import AsyncIOMotorClient
from pymongo.errors import DuplicateKeyError


class RunOwnershipError(ValueError):
    """Run de outra sessão ou de outro ``user_id`` (pelo ``run_id`` ou pela sessão): não é gravado."""


class SessionDb(AsyncMongoDb):
    """``AsyncMongoDb`` do agno sobre um cliente motor que é dele: ``close`` fecha esse cliente.

    ``db_url`` só entra na semente do ``id`` do db (o mesmo que o ``MongoDb`` síncrono derivava de
    URL e banco: o ``db_id`` das rotas do AgentOS não muda); quem conecta é o ``client``.
    """

    def __init__(self, client: AsyncIOMotorClient[dict[str, Any]], *, db_url: str, db_name: str) -> None:
        super().__init__(db_client=client, db_url=db_url, db_name=db_name)
        self._owned_client = client
        self._closed = False

    async def upsert_run(
        self,
        run: RunOutput | TeamRunOutput | WorkflowRunOutput | dict[str, Any],
        session_id: str,
        user_id: str | None = None,
        run_index: int | None = None,
    ) -> None:
        """Grava o run, salvo se ele for de outro dono (``RunOwnershipError``).

        O agno loga a recusa (``aupsert_run``, aviso) e o run segue sem ser gravado.

        Mesmo corpo do ``upsert_run`` do agno 3.1.12 (``agno/db/mongo/async_mongo.py``), com o dono no
        filtro: ``{"run_id", "session_id", "user_id"}`` e ``upsert``. O ``run_id`` de outra sessão ou de
        outro ``user_id`` não casa o filtro, a inserção bate no índice único de ``run_id``
        (``agno/db/mongo/schemas.py``) e vira a recusa: conferir e gravar é uma operação só. Dois
        primeiros runs simultâneos do mesmo dono com o mesmo ``run_id`` também disputam a inserção e
        o segundo é recusado. Sem o índice (o agno só avisa quando não consegue criá-lo), o run de
        outro dono vira um segundo documento com o mesmo ``run_id``: o do dono segue intacto.

        A sessão já gravada com outro ``user_id`` também recusa (sessão sem dono aceita, como no
        ``upsert_session``); essa leitura é só do ``user_id`` da sessão e não é atômica com a
        gravação (a sessão nova disputada por dois usuários ao mesmo tempo).

        Não vê o run que só existe no campo legado ``runs`` da sessão (gravado pelo 2.5.8): o
        primeiro a gravar esse ``run_id`` em ``agno_runs`` fica com ele (pendência do F3-06).
        """
        sessions = await self._get_collection(table_type="sessions")
        session = await sessions.find_one({"session_id": session_id}, {"user_id": 1}) if sessions is not None else None
        owner = session.get("user_id") if isinstance(session, dict) else None
        if owner is not None and owner != user_id:
            raise RunOwnershipError("sessão de outro usuário; run não gravado")
        runs = await self._get_collection(table_type="runs", create_collection_if_not_found=True)
        if runs is None:
            return
        row = build_single_run_row(run=run, session_id=session_id, user_id=user_id, run_index=run_index)
        owned = {"run_id": row["run_id"], "session_id": session_id, "user_id": user_id}
        existing = await runs.find_one(owned, {"run_index": 1})
        if existing is not None and "run_index" in existing:
            row["run_index"] = existing["run_index"]
        try:
            await runs.replace_one(owned, row, upsert=True)
        except DuplicateKeyError as exc:
            raise RunOwnershipError("run_id já gravado em outra sessão ou de outro usuário; run não gravado") from exc

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
