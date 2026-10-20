"""``AgnoRuntime.swap`` (FA-04): as rotas do AgentOS, do AG-UI e de cancel passam a servir o conjunto novo.

AgentOS real num FastAPI nu (sem a borda), com ``Agent``/``Team`` de ``FakeChatModel``. O run em andamento
durante a troca está no teste de integração (``tests/integration/test_fa04_hot_swap.py``).
"""

from __future__ import annotations

from typing import Any

import pytest
from agno.agent import Agent
from agno.team import Team
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.infrastructure.runtime.agno import AgnoRuntime
from tests.fakes import FakeChatModel
from tests.fakes.session_db import fake_session_db
from tests.fakes.web import agno_runtime


def _agent(agent_id: str, reply: str = "oi") -> Agent:
    return Agent(id=agent_id, name=agent_id, model=FakeChatModel(responses=[reply]), telemetry=False)


def _team(team_id: str, members: list[Agent]) -> Team:
    return Team(id=team_id, name=team_id, members=members, model=FakeChatModel(responses=[]), telemetry=False)


def _agui_body() -> dict[str, Any]:
    return {
        "threadId": "t1",
        "runId": "r1",
        "state": {},
        "messages": [{"id": "m1", "role": "user", "content": "oi"}],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }


def _ids(response: Any) -> list[str]:
    return [item["id"] for item in response.json()]


@pytest.fixture
def runtime() -> AgnoRuntime:
    return agno_runtime(fake_session_db())


@pytest.fixture
def client(runtime: AgnoRuntime) -> TestClient:
    app = FastAPI()
    first = _agent("velho", reply="do velho")
    runtime.mount(app, [first], [_team("time-velho", [first])], cors_allowed_origins=[])
    return TestClient(app)


def test_rotas_do_agentos_servem_o_conjunto_novo_e_a_entidade_removida_some(runtime: AgnoRuntime, client: TestClient):
    new = _agent("novo", reply="do novo")

    assert runtime.swap([new], [_team("time-novo", [new])]) == 1

    assert _ids(client.get("/agents")) == ["novo"]
    assert _ids(client.get("/teams")) == ["time-novo"]
    assert client.get("/agents/velho").status_code == 404
    assert client.post("/agents/velho/runs", data={"message": "oi", "stream": "false"}).status_code == 404
    assert client.get("/teams/time-velho").status_code == 404
    run = client.post("/agents/novo/runs", data={"message": "oi", "stream": "false"})
    assert run.status_code == 200 and run.json()["content"] == "do novo"


def test_agui_serve_a_entidade_criada_depois_e_responde_404_para_a_removida(runtime: AgnoRuntime, client: TestClient):
    runtime.swap([_agent("novo", reply="do novo")], [])

    created = client.post("/agui/novo", json=_agui_body())
    alias = client.post("/agui", json=_agui_body())

    assert created.status_code == 200 and "do novo" in created.text
    assert client.post("/agui/velho", json=_agui_body()).status_code == 404
    assert client.post("/agui/time-velho", json=_agui_body()).status_code == 404
    assert alias.headers["link"] == '</agui/novo>; rel="successor-version"'


def test_cancel_da_entidade_removida_e_404_de_entidade_e_o_da_nova_chega_ao_gerenciador(
    runtime: AgnoRuntime, client: TestClient
):
    runtime.swap([_agent("novo")], [])

    removed = client.post("/agents/velho/runs/r1/cancel")
    removed_team = client.post("/teams/time-velho/runs/r1/cancel")
    new = client.post("/agents/novo/runs/r1/cancel")

    assert removed.status_code == removed_team.status_code == 404
    assert removed.json() == {"detail": "entidade não encontrada"}
    assert new.status_code == 404 and new.json() == {"detail": "run não encontrado ou já encerrado"}


def test_entidades_novas_sao_preparadas_como_as_do_startup(runtime: AgnoRuntime, client: TestClient):
    """O AgentOS liga ``store_events`` (exigido pelas rotas dele) e dá o db padrão a quem não tem."""
    new = _agent("novo")
    team = _team("time-novo", [new])

    runtime.swap([new], [team])

    assert new.store_events is True and team.store_events is True
    assert new.db is not None and team.db is not None


def test_troca_que_falha_no_preparo_nao_muda_nada(runtime: AgnoRuntime, client: TestClient):
    """Ids repetidos: o AgentOS recusa no preparo, antes de qualquer lista ser trocada."""
    with pytest.raises(ValueError, match="Duplicate IDs"):
        runtime.swap([_agent("dup"), _agent("dup")], [])

    assert runtime.revision == 0
    assert _ids(client.get("/agents")) == ["velho"]
    assert client.post("/agui/velho", json=_agui_body()).status_code == 200


def test_agentos_sem_uma_das_listas_recusa_a_troca_sem_mexer_na_outra(runtime: AgnoRuntime, client: TestClient):
    """Defesa: o mount sempre passa as duas listas; sem uma delas, nenhuma é reenchida (sem troca pela metade)."""
    runtime._agent_os.teams = None  # type: ignore[union-attr]  # estado que nenhuma API pública produz

    with pytest.raises(RuntimeError, match="sem a lista de entidades"):
        runtime.swap([_agent("novo")], [])

    assert runtime.revision == 0
    assert _ids(client.get("/agents")) == ["velho"]


def test_troca_para_nenhuma_entidade_esvazia_as_rotas(runtime: AgnoRuntime, client: TestClient):
    runtime.swap([], [])

    assert client.get("/agents").json() == [] and client.get("/teams").json() == []
    assert client.post("/agui", json=_agui_body()).status_code == 404


def test_montagem_sem_entidades_serve_as_criadas_depois(runtime: AgnoRuntime):
    """O startup monta o AgentOS mesmo vazio: a primeira entidade criada pela API já é servida."""
    app = FastAPI()
    runtime.mount(app, [], [], cors_allowed_origins=[])
    client = TestClient(app)

    runtime.swap([_agent("primeiro", reply="olá")], [])

    assert _ids(client.get("/agents")) == ["primeiro"]
    assert "olá" in client.post("/agui/primeiro", json=_agui_body()).text
