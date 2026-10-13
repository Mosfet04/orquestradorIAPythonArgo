"""F2-08: o composition root cria um db de sessões só, entrega-o às fábricas e ao runtime e o fecha.

Container real (``DependencyContainer``) sobre o ``startup_world``: só o Mongo (clientes) e o modelo
são falsos; o db de sessões é o ``SessionDb`` de produção sobre o cliente motor em memória.
"""

from __future__ import annotations

import pytest
from agno.agent import Agent
from agno.team import Team

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.team_config import TeamConfig
from src.infrastructure import dependency_injection as di
from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.runtime.agno.session_db import SessionDb
from tests.fakes import RecordingLogger
from tests.fakes.startup_world import CLEAN_ENV, Events, startup_world

MARKER = "mongodb://usuario:s3nh4-do-banco@mongo.invalid"


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in CLEAN_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")


def _agent(agent_id: str) -> AgentConfig:
    return AgentConfig(id=agent_id, nome=agent_id, factory_ia_model="ollama", model="m", descricao="d", prompt="p")


async def test_agentes_e_teams_do_container_usam_o_mesmo_db_de_sessoes() -> None:
    events = Events()
    with startup_world(events):
        container = await di.DependencyContainer.create_async(AppConfig.load())
        try:
            runtime = container.get_agent_runtime()
            first = await runtime.build_agent(_agent("a1"))
            second = await runtime.build_agent(_agent("a2"))
            team = runtime.build_team(
                TeamConfig(id="t1", nome="t1", factory_ia_model="ollama", model="m", member_ids=["a1", "a2"]),
                [first, second],
            )
        finally:
            await container.cleanup()

    assert isinstance(first, Agent) and isinstance(second, Agent) and isinstance(team, Team)
    assert isinstance(first.db, SessionDb)
    assert second.db is first.db and team.db is first.db
    assert events.count("db:created") == 1


async def test_cleanup_fecha_o_db_de_sessoes_e_o_cliente_do_container_uma_vez_e_fora_do_event_loop() -> None:
    events = Events()
    with startup_world(events) as motors:
        container = await di.DependencyContainer.create_async(AppConfig.load())
        assert [n for n in events.names if n.startswith(("db:close", "motor:close"))] == []

        await container.cleanup()
        first_cleanup = list(events.names)
        await container.cleanup()

    # "db:close"/"motor:close" e não "...:on-loop": nenhum close rodou na thread do event loop.
    assert [n for n in first_cleanup if n.startswith(("db:close", "motor:close"))] == ["db:close", "motor:close"]
    assert events.count("db:close") == 1  # o segundo cleanup não fecha o db de novo
    assert [m.closes for m in motors] == [2]  # o cliente do container segue como antes (close idempotente)
    assert not [n for n in events.names if n.endswith(":on-loop")]


async def test_falha_no_wiring_depois_de_criar_o_db_de_sessoes_fecha_o_db(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken_runtime(**_: object) -> None:
        raise RuntimeError("runtime quebrado")

    events = Events()
    with startup_world(events) as motors:
        monkeypatch.setattr(di, "AgnoRuntime", broken_runtime)
        with pytest.raises(RuntimeError, match="runtime quebrado"):
            await di.DependencyContainer.create_async(AppConfig.load())

    assert (events.count("db:created"), events.count("db:close")) == (1, 1)
    assert [m.closes for m in motors] == [1]
    assert not [n for n in events.names if n.endswith(":on-loop")]


async def test_falha_ao_fechar_o_db_de_sessoes_e_logada_so_pelo_tipo_e_o_cliente_do_container_fecha(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    logger = RecordingLogger()
    monkeypatch.setattr(di, "StructlogLoggerAdapter", lambda _name: logger)
    events = Events()
    with startup_world(events) as motors:
        container = await di.DependencyContainer.create_async(AppConfig.load())
        session_db = container._session_db
        assert session_db is not None

        def failing_close() -> None:
            raise OSError(MARKER)

        monkeypatch.setattr(session_db.db_client, "close", failing_close)

        await container.cleanup()

    warnings = [(r.message, r.context) for r in logger.records if r.level == "warning"]
    assert ("Falha ao fechar o db de sessões", {"error_type": "OSError"}) in warnings
    assert MARKER not in repr(logger.records)
    assert [m.closes for m in motors] == [1]


async def test_agente_e_team_do_container_continuam_a_mesma_conversa_no_mesmo_session_id() -> None:
    """Decisão do F2-08: como com o ``MongoDb`` síncrono, o histórico não se perde quando outro tipo
    de entidade recebe o ``session_id`` (agente, depois team, depois o agente de novo)."""
    events = Events()
    with startup_world(events):
        container = await di.DependencyContainer.create_async(AppConfig.load())
        try:
            runtime = container.get_agent_runtime()
            agent = await runtime.build_agent(_agent("a1"))
            team = runtime.build_team(
                TeamConfig(id="t1", nome="t1", factory_ia_model="ollama", model="m", member_ids=["a1"]), [agent]
            )
            assert isinstance(agent, Agent) and isinstance(team, Team)
            await agent.arun("meu código de cliente é 4815", user_id="ana", session_id="s1")
            await team.arun("qual é o meu código?", user_id="ana", session_id="s1")
            # Instância nova para o run avulso, como a rota REST do AgentOS (cópia sem team_id): o agno
            # não grava sessão própria de um Agent que já foi inicializado como membro de um team.
            standalone = await runtime.build_agent(_agent("a1"))
            assert isinstance(standalone, Agent)
            await standalone.arun("e agora?", user_id="ana", session_id="s1")
            session_db = container._session_db
            assert session_db is not None
            docs = list(session_db.db_client[session_db.db_name]["agno_sessions"].docs)
        finally:
            await container.cleanup()

    assert [(d["session_id"], d["user_id"], d["session_type"]) for d in docs] == [("s1", "ana", "agent")]
    runs = [(r.get("agent_id"), r.get("team_id")) for r in docs[0]["runs"]]
    assert runs == [("a1", None), (None, "t1"), ("a1", None)]
    assert docs[0]["runs"][0]["input"]["input_content"] == "meu código de cliente é 4815"
