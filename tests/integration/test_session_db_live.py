"""F2-08, ``live``: o db de sessões (``SessionDb``) contra um Mongo de verdade.

Só rodam com ``MONGO_LIVE_URL`` (marker ``live``; nunca na CI padrão); sem ela são pulados. Cada
teste usa um banco efêmero de nome único, apagado no fim (``tests/fakes/live_mongo.py``). O modelo é
o ``FakeChatModel``: nenhuma chamada a LLM.

Cobrem o que o cliente em memória não prova: o driver (motor) com o ``AsyncMongoDb`` do agno 2.5.8,
os índices reais e a compatibilidade com o que o ``MongoDb`` síncrono (de antes do F2-08) gravou.
``MONGO_LIVE_URL`` não entra em ``assert``, id de teste, log nem ``repr``: os dbs daqui recebem uma
URL fictícia na semente do id e o cliente já pronto.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from typing import Any

import pytest
from agno.agent import Agent
from agno.db.base import SessionType
from agno.db.mongo import MongoDb
from agno.db.schemas import UserMemory
from agno.models.response import ModelResponse
from agno.run import RunStatus
from agno.run.cancel import get_cancellation_manager
from agno.session import AgentSession
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
from tests.fakes.live_mongo import LiveMongo

pytestmark = pytest.mark.live

# Só a semente do id do db (o cliente vem pronto): nunca a URL real.
PLACEHOLDER_URL = "mongodb://mongo.live.invalid:27017"
ANA_FACT = "Ana prefere respostas em espanhol"
BRUNO_FACT = "Bruno é alérgico a amendoim"


@pytest.fixture(autouse=True)
def _no_agno_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AGNO_TELEMETRY", "false")


def _session_db(live: LiveMongo) -> SessionDb:
    """Um ``SessionDb`` novo (cliente motor novo), como um processo que acabou de subir."""
    return SessionDb(live.motor(), db_url=PLACEHOLDER_URL, db_name=live.database)


def _sync_db(live: LiveMongo) -> MongoDb:
    """O ``MongoDb`` síncrono do agno, o db de sessões de antes do F2-08."""
    return MongoDb(db_client=live.sync(), db_name=live.database)


async def _agent(db: Any, agent_id: str = "agente", *, memory: bool = False) -> Agent:
    logger = RecordingLogger()
    service = AgentFactoryService(
        db_url=PLACEHOLDER_URL,
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
            user_memory_active=memory,
        )
    )


def _team(db: Any, member: Agent) -> Team:
    service = TeamFactoryService(
        session_db=db, logger=RecordingLogger(), model_factory=FakeModelFactory(responses=["resposta do time"] * 10)
    )
    return service.create_team(
        TeamConfig(
            id="time", nome="time", factory_ia_model="fake", model="mt", member_ids=[member.id or ""],
            mode="coordinate",
        ),
        [member],
    )


def _prompt(entity: Agent | Team) -> str:
    """Tudo o que o modelo da entidade recebeu (histórico e memórias entram aqui)."""
    model = entity.model
    assert isinstance(model, FakeChatModel)
    return "\n".join(content for call in model.calls for _, content in call.messages)


def _sessions(live: LiveMongo) -> list[dict[str, Any]]:
    return list(live.collection("agno_sessions").find({}).sort("session_id", 1))


def _memories(live: LiveMongo) -> list[tuple[str | None, str | None]]:
    return sorted((d.get("user_id"), d.get("memory")) for d in live.collection("agno_memories").find({}))


def _index_keys(live: LiveMongo, collection: str) -> dict[str, tuple[list[tuple[str, int]], bool]]:
    """``nome do índice -> (chaves, único)`` como está no servidor."""
    information = live.collection(collection).index_information()
    return {name: (list(spec["key"]), bool(spec.get("unique", False))) for name, spec in information.items()}


# ── sessão ───────────────────────────────────────────────────────────


async def test_sessao_persiste_entre_processos_e_o_historico_volta_no_run_seguinte(live_mongo: LiveMongo) -> None:
    first = await _agent(_session_db(live_mongo))
    await first.arun("meu código de cliente é 4815", user_id="ana", session_id="s1")

    # Outro db e outro agente, como depois de reiniciar o app: o histórico só pode vir do Mongo.
    restarted_db = _session_db(live_mongo)
    second = await _agent(restarted_db)
    run = await second.arun("qual é o meu código?", user_id="ana", session_id="s1")

    assert run.status == RunStatus.completed
    assert "meu código de cliente é 4815" in _prompt(second)
    stored = _sessions(live_mongo)
    assert [(d["session_id"], d["session_type"], d["agent_id"], d["user_id"]) for d in stored] == [
        ("s1", "agent", "agente", "ana")
    ]
    assert len(stored[0]["runs"]) == 2
    loaded = await restarted_db.get_session(session_id="s1", session_type=SessionType.AGENT, user_id="ana")
    assert isinstance(loaded, AgentSession) and len(loaded.runs or []) == 2


async def test_sessao_de_um_usuario_nao_e_lida_nem_trocada_por_outro(live_mongo: LiveMongo) -> None:
    db = _session_db(live_mongo)
    agent = await _agent(db)
    await agent.arun("meu código de cliente é 4815", user_id="ana", session_id="s1")
    before = _sessions(live_mongo)

    other = await _agent(_session_db(live_mongo))
    run = await other.arun("qual é o código?", user_id="bruno", session_id="s1")

    assert run.status == RunStatus.completed
    assert "4815" not in _prompt(other)
    assert await db.get_session(session_id="s1", session_type=SessionType.AGENT, user_id="bruno") is None
    # O índice único de session_id e o filtro por usuário do upsert: o documento da Ana fica como estava.
    after = _sessions(live_mongo)
    assert [(d["user_id"], len(d["runs"])) for d in after] == [("ana", 1)]
    assert after[0]["runs"] == before[0]["runs"]


# ── memória por user_id ──────────────────────────────────────────────


async def test_memoria_e_lida_so_do_user_id_da_requisicao(live_mongo: LiveMongo) -> None:
    db = _session_db(live_mongo)
    await db.upsert_user_memory(UserMemory(memory=ANA_FACT, user_id="ana", topics=["idioma"]))
    await db.upsert_user_memory(UserMemory(memory=BRUNO_FACT, user_id="bruno"))
    agent = await _agent(db, memory=True)

    run = await agent.arun("o que você sabe de mim?", user_id="ana", session_id="s-ana")

    assert run.status == RunStatus.completed
    assert ANA_FACT in _prompt(agent) and BRUNO_FACT not in _prompt(agent)
    assert [(m.user_id, m.memory, m.topics) for m in await db.get_user_memories(user_id="ana")] == [
        ("ana", ANA_FACT, ["idioma"])
    ]
    assert _memories(live_mongo) == [("ana", ANA_FACT), ("bruno", BRUNO_FACT)]


async def test_run_sem_user_id_em_agente_com_memoria_e_recusado_sem_ler_nem_gravar(live_mongo: LiveMongo) -> None:
    db = _session_db(live_mongo)
    await db.upsert_user_memory(UserMemory(memory=ANA_FACT, user_id="ana"))
    await db.upsert_user_memory(UserMemory(memory="memória do pool legado", user_id="default"))
    agent = await _agent(db, memory=True)

    run = await agent.arun("o que você sabe de mim?", session_id="s-anon")

    assert run.status == RunStatus.error and "user_id" in str(run.content)
    assert isinstance(agent.model, FakeChatModel) and agent.model.calls == []
    assert _memories(live_mongo) == [("ana", ANA_FACT), ("default", "memória do pool legado")]


# ── cancelamento ─────────────────────────────────────────────────────


class _SlowModel(FakeChatModel):
    """Uma chamada de 1 s: dá tempo de cancelar; o agno confere o cancelamento ao voltar dela."""

    async def ainvoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        await asyncio.sleep(1.0)
        return ModelResponse(role="assistant", content="fim")


async def _wait_for(predicate: Callable[[], bool], what: str, limit: float = 10.0) -> None:
    for _ in range(int(limit / 0.01)):
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError(f"timeout esperando {what}")


async def test_run_cancelado_fica_gravado_como_cancelado_na_sessao_do_usuario(live_mongo: LiveMongo) -> None:
    agent = await _agent(_session_db(live_mongo))
    agent.model = _SlowModel()
    manager = get_cancellation_manager()

    running = asyncio.create_task(agent.arun("oi", user_id="ana", session_id="s-cancel"))
    await _wait_for(lambda: len(manager.get_active_runs()) == 1, "o run registrado no gerenciador")
    run_id = next(iter(manager.get_active_runs()))
    cancelled = await Agent.acancel_run(run_id)
    run = await asyncio.wait_for(running, timeout=15)

    assert cancelled is True
    assert run.status == RunStatus.cancelled
    assert manager.get_active_runs() == {}
    stored = _sessions(live_mongo)
    assert [(d["session_id"], d["user_id"]) for d in stored] == [("s-cancel", "ana")]
    assert [(r["run_id"], r["status"]) for r in stored[0]["runs"]] == [(run_id, "CANCELLED")]


# ── compatibilidade com o MongoDb síncrono (dados de antes do F2-08) ──


async def test_sessao_e_memoria_gravadas_pelo_mongodb_sincrono_sao_lidas_pelo_session_db(
    live_mongo: LiveMongo,
) -> None:
    def write_with_sync_db() -> None:
        sync_db = _sync_db(live_mongo)
        sync_db.upsert_session(
            AgentSession(session_id="s-antiga", agent_id="agente", user_id="ana", session_data={"origem": "sync"})
        )
        sync_db.upsert_user_memory(UserMemory(memory=ANA_FACT, user_id="ana", topics=["idioma"]))

    await asyncio.to_thread(write_with_sync_db)
    indexes_before = {name: _index_keys(live_mongo, name) for name in ("agno_sessions", "agno_memories")}

    db = _session_db(live_mongo)
    session = await db.get_session(session_id="s-antiga", session_type=SessionType.AGENT, user_id="ana")
    memories = await db.get_user_memories(user_id="ana")
    await db.upsert_session(
        AgentSession(session_id="s-nova", agent_id="agente", user_id="ana", session_data={"origem": "async"})
    )

    assert isinstance(session, AgentSession) and session.session_data == {"origem": "sync"}
    assert [(m.user_id, m.memory, m.topics) for m in memories] == [("ana", ANA_FACT, ["idioma"])]
    # Mesmas coleções e os mesmos índices: o SessionDb não cria índice novo nem conflita com os que havia.
    assert {name: _index_keys(live_mongo, name) for name in indexes_before} == indexes_before
    assert indexes_before["agno_sessions"]["session_id_1"] == ([("session_id", 1)], True)
    assert indexes_before["agno_memories"]["memory_id_1"] == ([("memory_id", 1)], True)


async def test_sessao_e_memoria_gravadas_pelo_session_db_sao_lidas_pelo_mongodb_sincrono(
    live_mongo: LiveMongo,
) -> None:
    """Caminho de volta (rollback para a versão anterior): o documento novo serve ao db antigo."""
    db = _session_db(live_mongo)
    await db.upsert_session(
        AgentSession(session_id="s1", agent_id="agente", user_id="ana", session_data={"origem": "async"})
    )
    await db.upsert_user_memory(UserMemory(memory=ANA_FACT, user_id="ana", topics=["idioma"]))

    def read_with_sync_db() -> tuple[Any, Any]:
        sync_db = _sync_db(live_mongo)
        return (
            sync_db.get_session(session_id="s1", session_type=SessionType.AGENT, user_id="ana"),
            sync_db.get_user_memories(user_id="ana"),
        )

    session, memories = await asyncio.to_thread(read_with_sync_db)

    assert isinstance(session, AgentSession) and session.session_data == {"origem": "async"}
    assert [(m.user_id, m.memory, m.topics) for m in memories] == [("ana", ANA_FACT, ["idioma"])]


async def test_documentos_do_session_db_tem_os_mesmos_campos_dos_do_mongodb_sincrono(live_mongo: LiveMongo) -> None:
    def write_with_sync_db() -> None:
        sync_db = _sync_db(live_mongo)
        sync_db.upsert_session(AgentSession(session_id="s-sync", agent_id="agente", user_id="ana"))
        sync_db.upsert_user_memory(UserMemory(memory="m", user_id="sync"))

    await asyncio.to_thread(write_with_sync_db)
    db = _session_db(live_mongo)
    await db.upsert_session(AgentSession(session_id="s-async", agent_id="agente", user_id="ana"))
    await db.upsert_user_memory(UserMemory(memory="m", user_id="async"))

    sessions = {d["session_id"]: set(d) for d in _sessions(live_mongo)}
    memories = {d["user_id"]: set(d) for d in live_mongo.collection("agno_memories").find({})}
    assert sessions["s-async"] == sessions["s-sync"]
    assert memories["async"] == memories["sync"]


# ── o mesmo session_id com um agente e depois com um team (decisão do F2-08) ──


async def _agent_then_team(db_for_agent: Any, db_for_team: Any, session_id: str) -> tuple[Agent, Team]:
    agent = await _agent(db_for_agent)
    await agent.arun("meu código de cliente é 4815", user_id="ana", session_id=session_id)
    team = _team(db_for_team, await _agent(db_for_team, "membro"))
    await team.arun("qual é o meu código?", user_id="ana", session_id=session_id)
    return agent, team


async def test_team_que_reusa_o_session_id_de_um_agente_continua_a_conversa_como_no_mongodb_sincrono(
    live_mongo: LiveMongo,
) -> None:
    """Decisão do F2-08: manter o comportamento do ``MongoDb`` síncrono (``SessionDb.get_session`` lê
    sem filtrar por ``session_type``). O mesmo roteiro roda com os dois dbs e tem de dar o mesmo."""
    observed: dict[str, dict[str, object]] = {}
    for label, make_db in (("mongodb_sincrono", _sync_db), ("session_db", _session_db)):
        session_id = f"s-{label}"
        _, team = await _agent_then_team(make_db(live_mongo), make_db(live_mongo), session_id)
        docs = [d for d in _sessions(live_mongo) if d["session_id"] == session_id]
        assert len(docs) == 1  # o índice único de session_id deixa um documento só por sessão
        observed[label] = {
            "tipo_do_documento_final": docs[0]["session_type"],
            "runs_por_entidade": [(r.get("agent_id"), r.get("team_id")) for r in docs[0].get("runs") or []],
            "team_viu_a_mensagem_do_agente": "4815" in _prompt(team),
        }

    # O histórico do agente não se perde: o run dele continua no documento e o team o recebe.
    assert observed["session_db"] == {
        "tipo_do_documento_final": "team",
        "runs_por_entidade": [("agente", None), (None, "time")],
        "team_viu_a_mensagem_do_agente": True,
    }
    assert observed["session_db"] == observed["mongodb_sincrono"]
