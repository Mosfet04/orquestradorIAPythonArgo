"""F3-05: o AG-UI recusa ``threadId`` de sessão que já é de outro ``user_id`` (pendência (4) do F3-04).

No agno 3.x os runs vão para ``agno_runs`` sem o predicado de dono do ``upsert_session``: sem a
checagem, o turno do intruso entraria no histórico do dono. A recusa é um 404 antes do stream (o
modelo nem é chamado), como nas rotas REST do agno 3.x.
"""

from __future__ import annotations

import uuid

import httpx
import pytest
from ag_ui.core import EventType
from agno.agent import Agent
from fastapi import FastAPI

from src.infrastructure.runtime.agno.session_db import RunOwnershipError, SessionDb
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import FakeChatModel
from tests.fakes.agui import assert_valid_run, parse_agui_sse
from tests.fakes.session_db import collection, fake_session_db, session_db_client, stored_runs
from tests.fakes.web import mount_agent_os

RUN_KEY = "f305-run-key-" + "r" * 22
ADMIN_KEY = "f305-admin-key-" + "a" * 20
_ENV_NAMES = ("ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS", "APP_HOST", "API_KEY_RUN", "API_KEY_ADMIN")


@pytest.fixture
def session_db() -> SessionDb:
    return fake_session_db()


@pytest.fixture
def model() -> FakeChatModel:
    return FakeChatModel(responses=["resposta"] * 5)


@pytest.fixture
def app(monkeypatch: pytest.MonkeyPatch, session_db: SessionDb, model: FakeChatModel) -> FastAPI:
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    monkeypatch.setenv("APP_HOST", "0.0.0" + ".0")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("API_KEY_RUN", RUN_KEY)
    monkeypatch.setenv("API_KEY_ADMIN", ADMIN_KEY)
    factory = AppFactory()
    application = factory.create_app()
    agent = Agent(id="ag", name="ag", model=model, db=session_db, telemetry=False)
    mount_agent_os(factory, application, [agent], [], session_db=session_db)
    return application


async def _agui(
    app: FastAPI, thread_id: str, user_id: str | None, *, key: str = RUN_KEY, run_id: str = "r1"
) -> httpx.Response:
    body = {
        "threadId": thread_id,
        "runId": run_id,
        "state": {},
        "messages": [{"id": "m1", "role": "user", "content": "oi"}],
        "tools": [],
        "context": [],
        "forwardedProps": {} if user_id is None else {"user_id": user_id},
    }
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        return await client.post("/agui/ag", json=body, headers={"Authorization": f"Bearer {key}"})


def _finished(response: httpx.Response) -> bool:
    events = parse_agui_sse(response.text)
    assert_valid_run(events)
    return events[-1].type == EventType.RUN_FINISHED


@pytest.mark.parametrize(
    ("intruder", "key"),
    [("bruno", RUN_KEY), (None, RUN_KEY), ("bruno", ADMIN_KEY)],
    ids=["outro-user-id", "sem-user-id", "chave-admin-nao-e-bypass"],
)
async def test_thread_de_outro_dono_responde_404_antes_do_stream(
    app: FastAPI, session_db: SessionDb, model: FakeChatModel, intruder: str | None, key: str
) -> None:
    owner_run = await _agui(app, "s-ana", "ana")
    assert owner_run.status_code == 200 and _finished(owner_run), owner_run.text
    calls_before = len(model.calls)

    refused = await _agui(app, "s-ana", intruder, key=key)

    assert refused.status_code == 404
    assert refused.json() == {"detail": "Session not found"}
    assert len(model.calls) == calls_before  # recusado antes do run: o modelo não foi chamado
    assert len(await stored_runs(session_db, "s-ana", user_id="ana")) == 1


async def test_dono_segue_na_propria_thread_e_thread_nova_e_aceita(app: FastAPI, session_db: SessionDb) -> None:
    first = await _agui(app, "s-ana", "ana", run_id="r-ana-1")
    again = await _agui(app, "s-ana", "ana", run_id="r-ana-2")
    other = await _agui(app, "s-bruno", "bruno", run_id="r-bruno-1")

    assert [first.status_code, again.status_code, other.status_code] == [200, 200, 200]
    assert all(_finished(response) for response in (first, again, other))
    assert len(await stored_runs(session_db, "s-ana", user_id="ana")) == 2
    assert len(await stored_runs(session_db, "s-bruno", user_id="bruno")) == 1


async def test_run_id_de_outra_sessao_nao_regrava_o_run_do_dono(app: FastAPI, session_db: SessionDb) -> None:
    """Agno 3.x grava o run em ``agno_runs`` por ``run_id`` só: o ``runId`` da Ana numa thread do Bruno
    trocaria o documento do run dela (que some do histórico). O AG-UI troca o ``runId`` já gravado por
    um UUID4 (o run do Bruno é gravado com ele); a rota REST do agno roda o run sem gravá-lo."""
    owner_run = await _agui(app, "s-ana", "ana", run_id="r-ana")
    intruder = await _agui(app, "s-bruno", "bruno", run_id="r-ana")
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        rest = await client.post(
            "/agents/ag/runs",
            data={"message": "do bruno", "user_id": "bruno", "session_id": "s-bruno-2", "stream": "false",
                  "run_id": "r-ana"},
            headers={"Authorization": f"Bearer {RUN_KEY}"},
        )

    assert owner_run.status_code == 200 and _finished(owner_run), owner_run.text
    assert intruder.status_code == 200 and _finished(intruder) and rest.status_code == 200
    [ana_run] = await stored_runs(session_db, "s-ana", user_id="ana")
    assert (ana_run["run_id"], ana_run["user_id"], ana_run["input"]) == ("r-ana", "ana", {"input_content": "oi"})
    started_run_id = parse_agui_sse(intruder.text)[0].run_id
    assert started_run_id != "r-ana" and str(uuid.UUID(started_run_id, version=4)) == started_run_id
    [bruno_run] = await stored_runs(session_db, "s-bruno", user_id="bruno")
    assert (bruno_run["run_id"], bruno_run["user_id"]) == (started_run_id, "bruno")
    assert await stored_runs(session_db, "s-bruno-2", user_id="bruno") == []


async def test_upsert_run_grava_atomicamente_pelo_dono_sem_ler_os_runs_da_sessao(session_db: SessionDb) -> None:
    """A gravação filtra por ``run_id``, ``session_id`` e ``user_id`` (sem checar antes e gravar depois)
    e o dono da sessão sai de uma leitura só do documento dela, sem os runs (nem o blob legado ``runs``)."""
    await session_db.upsert_run({"run_id": "r-1", "agent_id": "ag"}, "s-ana", user_id="ana", run_index=0)
    sessions = collection(session_db, session_db.session_table_name)
    sessions.docs.append({"session_id": "s-ana", "user_id": "ana", "runs": [{"run_id": "legado"}]})
    runs = collection(session_db, session_db.runs_table_name)
    sessions.queries.clear()
    runs.queries.clear()

    await session_db.upsert_run({"run_id": "r-1", "agent_id": "ag", "content": "nova"}, "s-ana", user_id="ana")

    owned = {"run_id": "r-1", "session_id": "s-ana", "user_id": "ana"}
    assert runs.queries == [owned, owned]
    assert sessions.queries == [{"session_id": "s-ana"}]
    [doc] = runs.docs
    assert (doc["run_data"]["content"], doc["run_index"]) == ("nova", 0)


@pytest.mark.parametrize(
    ("session_id", "user_id"), [("s-bruno", "ana"), ("s-ana", "bruno"), ("s-ana", None)],
    ids=["outra-sessao", "outro-user-id", "sem-user-id"],
)
async def test_upsert_run_de_run_id_de_outro_dono_e_recusado_pelo_indice_unico(
    session_db: SessionDb, session_id: str, user_id: str | None
) -> None:
    await session_db.upsert_run({"run_id": "r-1", "agent_id": "ag", "content": "da ana"}, "s-ana", user_id="ana")
    runs = collection(session_db, session_db.runs_table_name)
    sessions = collection(session_db, session_db.session_table_name)
    sessions.docs.clear()  # sem sessão gravada: só o índice único de run_id recusa

    with pytest.raises(RunOwnershipError):
        await session_db.upsert_run({"run_id": "r-1", "agent_id": "ag"}, session_id, user_id=user_id)

    [doc] = runs.docs
    assert (doc["session_id"], doc["user_id"], doc["run_data"]["content"]) == ("s-ana", "ana", "da ana")


async def test_upsert_run_em_sessao_de_outro_dono_e_recusado_tambem_depois_de_um_restart(session_db: SessionDb) -> None:
    """Processo novo sobre o mesmo banco: a sessão é lida mesmo sem ter sido usada neste processo
    (o agno só devolve a coleção sem ``create`` depois de inicializá-la)."""
    owner_run = {"run_id": "r-ana", "agent_id": "ag", "content": "da ana"}
    await session_db.upsert_run(owner_run, "s-ana", user_id="ana", run_index=0)
    collection(session_db, session_db.session_table_name).docs.append({"session_id": "s-ana", "user_id": "ana"})
    restarted = fake_session_db(session_db_client(session_db))

    with pytest.raises(RunOwnershipError):
        await restarted.upsert_run({"run_id": "r-bruno", "agent_id": "ag"}, "s-ana", user_id="bruno", run_index=1)

    assert [doc["run_id"] for doc in collection(session_db, session_db.runs_table_name).docs] == ["r-ana"]
