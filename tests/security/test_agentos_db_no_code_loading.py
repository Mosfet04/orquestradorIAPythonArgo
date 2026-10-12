"""F2-08: com ``db=`` no AgentOS, só roda o que está no código.

O AgentOS do agno 2.5.8, com um ``BaseDb`` síncrono, procura no banco o agente/team que não está
entre os montados (``agno/os/utils.py``, ``get_agent_by_id``/``get_team_by_id``) e ``/components``
grava essas definições: elas rodariam fora do ``ProviderRegistry`` (destino, ``api_key_ref``, guarda
SSRF) e do guardrail de ``user_id``. O db de sessões é assíncrono (``SessionDb``), caminho em que o
agno não consulta o banco, e o ``AgnoRuntime`` recusa um db síncrono.
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from agno.agent import Agent
from agno.db.in_memory import InMemoryDb
from agno.team import Team
from starlette.testclient import TestClient

from src.infrastructure.runtime.agno.session_db import SessionDb
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import FakeChatModel
from tests.fakes.session_db import fake_session_db, session_db_client
from tests.fakes.web import agno_runtime, mount_agent_os

RUN_KEY = "qa-run-key-" + "r" * 21
ADMIN_KEY = "qa-admin-key-" + "a" * 19
RUN = {"Authorization": f"Bearer {RUN_KEY}"}
ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}
ALL_INTERFACES = "0.0.0" + ".0"  # bind recusado sem chaves: o app sobe em modo produção
_ENV_NAMES = ("ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS", "APP_HOST", "API_KEY_RUN", "API_KEY_ADMIN")
GHOST = "fantasma"


def _component_documents(entity_id: str) -> dict[str, list[dict[str, object]]]:
    """Definições gravadas direto nas coleções de componentes do agno, como ``/components`` faria."""
    return {
        "agno_components": [
            {"component_id": entity_id, "component_type": "agent", "name": entity_id, "current_version": 1},
            {"component_id": f"{entity_id}-time", "component_type": "team", "name": entity_id, "current_version": 1},
        ],
        "agno_component_configs": [
            {"component_id": entity_id, "version": 1, "stage": "published", "config": {"id": entity_id}},
        ],
    }


@pytest.fixture
def session_db() -> SessionDb:
    db = fake_session_db()
    database = session_db_client(db)[db.db_name]
    for name, documents in _component_documents(GHOST).items():
        database[name].docs.extend(documents)
    return db


@pytest.fixture
def client(monkeypatch: pytest.MonkeyPatch, session_db: SessionDb) -> Iterator[TestClient]:
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    monkeypatch.setenv("APP_HOST", ALL_INTERFACES)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("API_KEY_RUN", RUN_KEY)
    monkeypatch.setenv("API_KEY_ADMIN", ADMIN_KEY)
    agent = Agent(id="do-codigo", name="do-codigo", model=FakeChatModel(responses=["ok"] * 5), db=session_db,
                  telemetry=False)
    team = Team(id="time-do-codigo", name="t", members=[agent], model=FakeChatModel(responses=["ok"] * 5),
                db=session_db, telemetry=False)
    factory = AppFactory()
    app = factory.create_app()
    mount_agent_os(factory, app, [agent], [team], session_db=session_db)
    # Sem ``with``: o lifespan do app (container, Mongo) não roda; as rotas já estão montadas.
    http = TestClient(app, raise_server_exceptions=False)
    yield http
    http.close()


def _queries(db: SessionDb) -> dict[str, int]:
    """Quantas consultas cada coleção do db recebeu."""
    database = session_db_client(db)[db.db_name]
    return {name: len(collection.queries) for name, collection in database.collections.items()}


def test_o_agentos_recebe_o_db_de_sessoes(client: TestClient, session_db: SessionDb) -> None:
    config = client.get("/config", headers=RUN).json()

    assert (config["os_database"], config["databases"]) == (session_db.id, [session_db.id])


def test_agente_e_team_gravados_no_banco_nao_sao_listados(client: TestClient, session_db: SessionDb) -> None:
    before = _queries(session_db)

    agents = client.get("/agents", headers=RUN)
    teams = client.get("/teams", headers=RUN)

    assert [a["id"] for a in agents.json()] == ["do-codigo"]
    assert [t["id"] for t in teams.json()] == ["time-do-codigo"]
    assert _queries(session_db) == before  # nenhuma consulta ao banco para montar as listas


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", f"/agents/{GHOST}"),
        ("POST", f"/agents/{GHOST}/runs"),
        ("GET", f"/agents/{GHOST}/runs"),
        ("POST", f"/agents/{GHOST}/runs/r1/continue"),
        ("POST", f"/agents/{GHOST}/runs/r1/cancel"),
        ("GET", f"/teams/{GHOST}-time"),
        ("POST", f"/teams/{GHOST}-time/runs"),
        ("POST", f"/teams/{GHOST}-time/runs/r1/cancel"),
        ("POST", f"/agui/{GHOST}"),
    ],
)
def test_entidade_que_so_existe_no_banco_e_404_sem_consulta_ao_banco(
    client: TestClient, session_db: SessionDb, method: str, path: str
) -> None:
    before = _queries(session_db)
    agui = {"threadId": "t1", "runId": "r1", "state": {}, "messages": [], "tools": [], "context": [],
            "forwardedProps": {}}
    body = {"json": agui} if path.startswith("/agui") else {"data": {"message": "oi", "tools": "[]"}}

    extra = body if method == "POST" else {"params": {"session_id": "s1"}}

    response = client.request(method, path, headers=RUN, **extra)  # type: ignore[arg-type]

    assert response.status_code == 404, (method, path, response.text[:200])
    assert _queries(session_db) == before


def test_entidade_do_codigo_roda_e_persiste_no_mesmo_db(client: TestClient, session_db: SessionDb) -> None:
    run = client.post(
        "/agents/do-codigo/runs",
        data={"message": "oi", "stream": "false", "user_id": "ana", "session_id": "s1"},
        headers=RUN,
    )

    assert run.status_code == 200 and run.json()["status"] == "COMPLETED"
    sessions = session_db_client(session_db)[session_db.db_name]["agno_sessions"].docs
    assert [(d["session_id"], d["agent_id"], d["user_id"]) for d in sessions] == [("s1", "do-codigo", "ana")]


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "PATCH", "DELETE"])
@pytest.mark.parametrize("path", ["/components", f"/components/{GHOST}", f"/components/{GHOST}/configs"])
def test_components_e_stub_503_so_para_admin_e_nao_grava_nada(
    client: TestClient, session_db: SessionDb, method: str, path: str
) -> None:
    database = session_db_client(session_db)[session_db.db_name]
    before = {name: list(collection.docs) for name, collection in database.collections.items()}

    as_run = client.request(method, path, headers=RUN, json={"name": "x", "component_type": "agent"})
    as_admin = client.request(method, path, headers=ADMIN, json={"name": "x", "component_type": "agent"})

    assert as_run.status_code == 403
    assert as_admin.status_code == 503
    assert "sync db" in as_admin.json()["detail"]  # o stub do agno para db assíncrono
    assert {name: list(collection.docs) for name, collection in database.collections.items()} == before


def test_runtime_recusa_db_de_sessoes_sincrono() -> None:
    with pytest.raises(TypeError, match="assíncrono"):
        agno_runtime(InMemoryDb())  # type: ignore[arg-type]
