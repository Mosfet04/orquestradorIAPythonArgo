"""QA do F2-09: ``AgnoRuntime.mount`` depois de extrair ``_build_agent_os``.

O AgentOS é construído com a configuração de segurança e de ciclo de vida do projeto (sem
telemetria nem tracing, sem provisionar coleções no startup, db de sessões compartilhado, rotas do
app preservadas). Os testes existentes provam rotas e ``telemetry``; aqui, o resto dos argumentos,
e que a falha do construtor acontece antes de qualquer efeito no app (ordem do ``mount``).
"""

from __future__ import annotations

from typing import Any, ClassVar

import pytest
from agno.agent import Agent
from agno.os import AgentOS
from agno.run.cancel import get_cancellation_manager
from agno.team import Team
from fastapi import FastAPI

from src.infrastructure.runtime.agno.run_cancellation import RegisteredRunCancellationManager
from src.infrastructure.runtime.agno.session_db import SessionDb
from tests.fakes import FakeChatModel
from tests.fakes.session_db import fake_session_db
from tests.fakes.web import agno_runtime

RUNTIME_MODULE = "src.infrastructure.runtime.agno.runtime"


class _RecordingAgentOS(AgentOS):
    created: ClassVar[list[AgentOS]] = []
    received: ClassVar[list[dict[str, Any]]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        _RecordingAgentOS.received.append(kwargs)
        super().__init__(*args, **kwargs)
        _RecordingAgentOS.created.append(self)


@pytest.fixture(autouse=True)
def _reset_recording(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(_RecordingAgentOS, "created", [])
    monkeypatch.setattr(_RecordingAgentOS, "received", [])


def _agent(agent_id: str = "a1") -> Agent:
    return Agent(id=agent_id, name=agent_id, model=FakeChatModel(responses=[]), telemetry=False)


def _team(agent: Agent) -> Team:
    return Team(id="t1", name="t1", members=[agent], model=FakeChatModel(responses=[]), telemetry=False)


def test_agent_os_recebe_a_configuracao_do_projeto(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(f"{RUNTIME_MODULE}.AgentOS", _RecordingAgentOS)
    session_db: SessionDb = fake_session_db()
    runtime = agno_runtime(session_db)
    app = FastAPI()
    agent = _agent()
    team = _team(agent)
    origins = ["https://painel.example.com", "https://outro.example.com"]

    runtime.mount(app, [agent], [team], cors_allowed_origins=origins)

    (agent_os,) = _RecordingAgentOS.created
    assert agent_os.db is session_db
    assert agent_os.base_app is app
    assert agent_os.on_route_conflict == "preserve_base_app"
    assert (agent_os.telemetry, agent_os.tracing, agent_os.auto_provision_dbs) == (False, False, False)
    assert agent_os.agents == [agent] and agent_os.teams == [team]
    assert agent_os.cors_allowed_origins == origins


def test_sem_teams_o_agent_os_recebe_lista_vazia_e_as_listas_dele_sao_copias(monkeypatch: pytest.MonkeyPatch) -> None:
    """Lista vazia (não ``None``): a troca a quente (FA-04) reenche no lugar a lista que o AgentOS guardou."""
    monkeypatch.setattr(f"{RUNTIME_MODULE}.AgentOS", _RecordingAgentOS)
    runtime = agno_runtime(fake_session_db())
    agents = [_agent("a1"), _agent("a2")]

    runtime.mount(FastAPI(), agents, [], cors_allowed_origins=[])

    (kwargs,) = _RecordingAgentOS.received
    assert kwargs["teams"] == []
    assert kwargs["agents"] == agents and kwargs["agents"] is not agents  # o AgentOS não muta a lista de quem montou


def test_sem_db_de_sessoes_o_agent_os_sobe_sem_db(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(f"{RUNTIME_MODULE}.AgentOS", _RecordingAgentOS)

    agno_runtime(None).mount(FastAPI(), [_agent()], [], cors_allowed_origins=[])

    (agent_os,) = _RecordingAgentOS.created
    assert agent_os.db is None


def test_construtor_do_agent_os_que_falha_nao_deixa_efeito_no_app_nem_instala_o_gerenciador_de_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ordem do ``mount`` é: construir o AgentOS (valida ids) e só depois instalar o gerenciador e as rotas."""

    class BrokenAgentOS:
        def __init__(self, *_: Any, **__: Any) -> None:
            raise ValueError("ids duplicados")

    runtime = agno_runtime(fake_session_db())
    app = FastAPI()
    routes_before = list(app.routes)
    lifespan_before = app.router.lifespan_context
    manager_before = get_cancellation_manager()
    monkeypatch.setattr(f"{RUNTIME_MODULE}.AgentOS", BrokenAgentOS)

    with pytest.raises(ValueError, match="ids duplicados"):
        runtime.mount(app, [_agent()], [], cors_allowed_origins=[])

    assert app.routes == routes_before
    assert app.router.lifespan_context is lifespan_before
    assert get_cancellation_manager() is manager_before
    assert not isinstance(get_cancellation_manager(), RegisteredRunCancellationManager)
    monkeypatch.undo()
    runtime.mount(app, [_agent()], [], cors_allowed_origins=[])  # a falha não marcou o runtime como montado
    assert isinstance(get_cancellation_manager(), RegisteredRunCancellationManager)
