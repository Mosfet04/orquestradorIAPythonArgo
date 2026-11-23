"""QA do F3-05: dono de sessão e de run no agno 3.1.12, pelo app real (REST e AG-UI) e pelo ``SessionDb``.

Complementa ``test_agui_session_owner.py`` (agente pelo AG-UI) e ``test_qa_f1_06_identity_e2e.py``
(outro ``user_id`` na rota REST de agente) com o que ficou de fora: anônimo e chave admin no REST,
SSE, team (inclusive com membro delegado), alias ``/agui``, ``user_id`` hostil, ``runId`` repetido
pelo próprio dono, sessão sem dono, documento de sessão gravado pelo 2.5.8, falha do Mongo na
conferência e as rotas novas do 3.x que leem ou copiam uma sessão.
"""

from __future__ import annotations

import copy
import json
import uuid
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from ag_ui.core import EventType
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.run.cancel import get_cancellation_manager
from agno.team import Team
from fastapi import FastAPI

from src.infrastructure.runtime.agno.session_db import RunOwnershipError, SessionDb
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import FakeChatModel
from tests.fakes.agui import assert_valid_run, parse_agui_sse
from tests.fakes.mongo import FailingMongoCollection
from tests.fakes.session_db import collection, fake_session_db, session_docs, stored_runs
from tests.fakes.web import mount_agent_os

RUN_KEY = "f305-qa-run-key-" + "r" * 20
ADMIN_KEY = "f305-qa-admin-key-" + "a" * 20
RUN = {"Authorization": f"Bearer {RUN_KEY}"}
ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}
NOT_FOUND = {"detail": "Session not found"}
HISTORY_MARK = "O-COFRE-DA-ANA-ABRE-COM-7421"
# Texto de teste no formato de um erro de driver com URI (montado em partes: não é credencial).
DRIVER_TEXT = "sem rota: mongodb://" + "usuario:" + "SENHA-DO-MONGO" + "@mongo.interno:27017"
_ENV_NAMES = ("ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS", "APP_HOST", "API_KEY_RUN", "API_KEY_ADMIN")


def _delegate(task: str, call_id: str) -> ModelResponse:
    call = {
        "id": call_id,
        "type": "function",
        "function": {"name": "delegate_task_to_member", "arguments": json.dumps({"member_id": "mb", "task": task})},
    }
    return ModelResponse(role="assistant", tool_calls=[call])


@dataclass
class World:
    app: FastAPI
    db: SessionDb
    agent_model: FakeChatModel
    member_model: FakeChatModel
    team_model: FakeChatModel

    def client(self, key: dict[str, str] | None = None) -> httpx.AsyncClient:
        """Cliente HTTP do app; sem ``key``, com a chave run."""
        # Sem re-levantar a exceção do app: o que se confere é a resposta que o cliente recebe.
        transport = httpx.ASGITransport(app=self.app, raise_app_exceptions=False)
        return httpx.AsyncClient(transport=transport, base_url="http://test", headers=RUN if key is None else key)

    def model_calls(self) -> int:
        return len(self.agent_model.calls) + len(self.member_model.calls) + len(self.team_model.calls)


@pytest.fixture
def world(monkeypatch: pytest.MonkeyPatch) -> World:
    """App de produção (chaves run e admin) com agente ``ag``, membro ``mb`` e team ``tm`` no mesmo db."""
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    monkeypatch.setenv("APP_HOST", "0.0.0" + ".0")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("API_KEY_RUN", RUN_KEY)
    monkeypatch.setenv("API_KEY_ADMIN", ADMIN_KEY)
    db = fake_session_db()
    agent_model = FakeChatModel(responses=["resposta"] * 10)
    member_model = FakeChatModel(responses=["do membro"] * 10)
    team_model = FakeChatModel(
        responses=[_delegate("primeira", "c1"), "o time respondeu", _delegate("segunda", "c2"), "o time de novo"]
    )
    agent = Agent(id="ag", name="ag", model=agent_model, db=db, telemetry=False, add_history_to_context=True)
    member = Agent(id="mb", name="mb", model=member_model, db=db, telemetry=False)
    team = Team(id="tm", name="tm", members=[member], model=team_model, db=db, telemetry=False)
    factory = AppFactory()
    app = factory.create_app()
    mount_agent_os(factory, app, [agent], [team], session_db=db)
    return World(app, db, agent_model, member_model, team_model)


def _agui_body(thread_id: str, user_id: Any, run_id: str = "r1", message: str = "oi") -> dict[str, Any]:
    return {
        "threadId": thread_id,
        "runId": run_id,
        "state": {},
        "messages": [{"id": "m1", "role": "user", "content": message}],
        "tools": [],
        "context": [],
        "forwardedProps": {} if user_id is None else {"user_id": user_id},
    }


def _form(session_id: str, user_id: str | None, message: str = "oi", **extra: str) -> dict[str, str]:
    form = {"message": message, "session_id": session_id, "stream": "false", **extra}
    return form if user_id is None else {**form, "user_id": user_id}


def _finished(response: httpx.Response) -> list[Any]:
    assert response.status_code == 200, response.text
    events = parse_agui_sse(response.text)
    assert_valid_run(events)
    assert events[-1].type == EventType.RUN_FINISHED, response.text
    return events


async def _snapshot(db: SessionDb, session_id: str, owner: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Documento da sessão e runs gravados (API pública do db), para comparar antes e depois."""
    docs = [doc for doc in session_docs(db) if doc["session_id"] == session_id]
    return copy.deepcopy(docs), copy.deepcopy(await stored_runs(db, session_id, user_id=owner))


# ── REST: o que o 404 de dono não pode deixar passar ─────────────────────────────────────────────


@pytest.mark.parametrize(
    ("path", "form", "key"),
    [
        ("/agents/ag/runs", _form("s-ana", None), RUN),
        ("/agents/ag/runs", _form("s-ana", ""), RUN),
        ("/agents/ag/runs", _form("s-ana", "bruno"), ADMIN),
        ("/agents/ag/runs", _form("s-ana", None), ADMIN),
        ("/agents/ag/runs", _form("s-ana", "bruno", stream="true"), RUN),
        ("/agents/ag/runs", _form("s-ana", "bruno", background="true"), RUN),
        ("/agents/ag/runs", _form("s-ana", "bruno", run_id="r-novo-do-bruno"), RUN),
        ("/agents/ag/runs", _form("s-ana", "Ana"), RUN),
        ("/agents/ag/runs", _form("s-ana", "ana "), RUN),
        ("/teams/tm/runs", _form("s-ana", "bruno"), RUN),
        ("/teams/tm/runs", _form("s-ana", None), ADMIN),
    ],
    ids=[
        "anonimo",
        "user-id-vazio",
        "chave-admin-nao-e-bypass",
        "chave-admin-anonima",
        "stream-sse",
        "background",
        "run-id-proprio",
        "outra-caixa",
        "espaco-no-fim",
        "team-na-sessao-do-agente",
        "team-admin-anonimo",
    ],
)
async def test_rest_recusa_sessao_de_outro_dono_antes_de_chamar_o_modelo(
    world: World, path: str, form: dict[str, str], key: dict[str, str]
) -> None:
    async with world.client() as client:
        owner = await client.post("/agents/ag/runs", data=_form("s-ana", "ana", HISTORY_MARK))
        assert owner.status_code == 200, owner.text
        before = await _snapshot(world.db, "s-ana", "ana")
        calls = world.model_calls()

        refused = await client.post(path, data=form, headers=key)

    assert (refused.status_code, refused.json()) == (404, NOT_FOUND)
    assert HISTORY_MARK not in refused.text
    assert world.model_calls() == calls  # recusado antes do run: nenhum modelo foi chamado
    assert await _snapshot(world.db, "s-ana", "ana") == before
    assert get_cancellation_manager().get_active_runs() == {}


# ── AG-UI: team, alias deprecated e user_id que não é o do dono ──────────────────────────────────


@pytest.mark.parametrize(
    ("path", "user_id"),
    [
        ("/agui/tm", "bruno"),
        ("/agui/tm", None),
        ("/agui", "bruno"),
        ("/agui/ag", ""),
        ("/agui/ag", "ana "),
        ("/agui/ag", "ANA"),
        ("/agui/ag", 7421),
        ("/agui/ag", ["ana"]),
        ("/agui/ag", {"$ne": None}),
        ("/agui/ag", True),
    ],
    ids=[
        "team-outro-user-id",
        "team-anonimo",
        "alias-agui",
        "user-id-vazio",
        "espaco-no-fim",
        "outra-caixa",
        "user-id-numero",
        "user-id-lista-com-o-dono",
        "user-id-operador-mongo",
        "user-id-booleano",
    ],
)
async def test_agui_recusa_thread_de_outro_dono_em_team_alias_e_user_id_hostil(
    world: World, path: str, user_id: Any
) -> None:
    """``forwardedProps.user_id`` que não é string vale como anônimo: não casa com o dono nem vira filtro."""
    async with world.client() as client:
        _finished(await client.post("/agui/ag", json=_agui_body("s-ana", "ana", "r-ana", HISTORY_MARK)))
        before = await _snapshot(world.db, "s-ana", "ana")
        calls = world.model_calls()

        refused = await client.post(path, json=_agui_body("s-ana", user_id, "r-intruso"))

    assert (refused.status_code, refused.json()) == (404, NOT_FOUND)
    assert refused.headers["content-type"].startswith("application/json")  # 404 de verdade, não um SSE com erro
    assert world.model_calls() == calls
    assert await _snapshot(world.db, "s-ana", "ana") == before
    assert get_cancellation_manager().get_active_runs() == {}
    assert [doc["run_id"] for doc in collection(world.db, world.db.runs_table_name).docs] == ["r-ana"]


async def test_team_com_membro_grava_os_runs_no_dono_e_recusa_o_intruso(world: World) -> None:
    """O run do membro delegado vai para ``agno_runs`` com a sessão e o ``user_id`` do pedido (não é
    recusado pelo ``upsert_run`` do ``SessionDb``), e o intruso não chega ao time nem ao membro."""
    async with world.client() as client:
        first = _finished(await client.post("/agui/tm", json=_agui_body("s-time", "ana", "t1", HISTORY_MARK)))
        team_calls, member_calls = len(world.team_model.calls), len(world.member_model.calls)

        by_agui = await client.post("/agui/tm", json=_agui_body("s-time", "bruno", "t-bruno"))
        by_rest = await client.post("/teams/tm/runs", data=_form("s-time", "bruno"))
        refused_calls = (len(world.team_model.calls), len(world.member_model.calls))

        second = await client.post("/teams/tm/runs", data=_form("s-time", "ana", "e agora?"))

    assert (team_calls, member_calls) == (2, 1)  # o time delegou e o membro respondeu
    # O terminal do membro (outro run_id) não vira o do run: o RUN_FINISHED leva o runId do RUN_STARTED.
    assert first[0].run_id == "t1" and first[-1].run_id == "t1"
    assert [event.type for event in first].count(EventType.RUN_FINISHED) == 1
    assert "o time respondeu" in "".join(getattr(event, "delta", "") or "" for event in first)
    for refused in (by_agui, by_rest):
        assert (refused.status_code, refused.json()) == (404, NOT_FOUND)
    assert refused_calls == (2, 1)
    assert second.status_code == 200 and second.json()["content"] == "o time de novo"
    runs = collection(world.db, world.db.runs_table_name).docs
    assert {(doc["session_id"], doc["user_id"]) for doc in runs} == {("s-time", "ana")}
    kinds = sorted((doc["run_type"], doc["run_data"].get("parent_run_id") is not None) for doc in runs)
    assert kinds == [("agent", True), ("agent", True), ("team", False), ("team", False)]
    assert "t-bruno" not in {doc["run_id"] for doc in runs}


# ── runId/run_id repetido pelo próprio dono ──────────────────────────────────────────────────────


async def test_agui_run_id_repetido_pelo_proprio_dono_nao_regrava_o_run_anterior(world: World) -> None:
    async with world.client() as client:
        first = _finished(await client.post("/agui/ag", json=_agui_body("s-ana", "ana", "mesmo", "primeira")))
        second = _finished(await client.post("/agui/ag", json=_agui_body("s-ana", "ana", "mesmo", "segunda")))

    assert first[0].run_id == "mesmo"
    replaced = second[0].run_id
    assert replaced != "mesmo" and str(uuid.UUID(replaced, version=4)) == replaced
    assert second[-1].run_id == replaced  # o terminal leva o runId devolvido no RUN_STARTED
    runs = await stored_runs(world.db, "s-ana", user_id="ana")
    assert [(run["run_id"], run["input"]["input_content"]) for run in runs] == [
        ("mesmo", "primeira"),
        (replaced, "segunda"),
    ]


async def test_rest_run_id_do_proprio_dono_na_mesma_sessao_regrava_e_em_outra_sessao_nao(world: World) -> None:
    """Contrato registrado no item: regravar o próprio run pelo REST segue valendo; o mesmo ``run_id``
    em outra sessão (mesmo do próprio dono) roda sem gravar e deixa o run original como estava."""
    async with world.client() as client:
        first = await client.post("/agents/ag/runs", data=_form("s-ana", "ana", "um", run_id="rw"))
        again = await client.post("/agents/ag/runs", data=_form("s-ana", "ana", "dois", run_id="rw"))
        elsewhere = await client.post("/agents/ag/runs", data=_form("s-ana-2", "ana", "tres", run_id="rw"))

    assert [first.status_code, again.status_code, elsewhere.status_code] == [200, 200, 200]
    runs = await stored_runs(world.db, "s-ana", user_id="ana")
    assert [(run["run_id"], run["input"]["input_content"]) for run in runs] == [("rw", "dois")]
    assert await stored_runs(world.db, "s-ana-2", user_id="ana") == []
    stored = collection(world.db, world.db.runs_table_name).docs
    assert [(doc["run_id"], doc["session_id"]) for doc in stored] == [("rw", "s-ana")]


# ── sessão sem dono ──────────────────────────────────────────────────────────────────────────────


async def test_sessao_anonima_fica_com_o_primeiro_user_id_e_fecha_para_os_demais_registro_de_risco(
    world: World,
) -> None:
    """Caracterização (predicado do agno, ``user_id IS NULL`` aceita): a sessão criada sem ``user_id``
    é de quem chegar primeiro com um; depois disso anônimo e terceiros recebem 404. O turno anônimo
    fica gravado na sessão que passou a ser do Bruno. Quem usa o app sem ``user_id`` não deve
    reaproveitar o ``session_id`` entre pessoas (identidade do principal: F5)."""
    async with world.client() as client:
        _finished(await client.post("/agui/ag", json=_agui_body("s-livre", None, "a1", "turno anonimo")))
        owner_before = [doc.get("user_id") for doc in session_docs(world.db)]

        _finished(await client.post("/agui/ag", json=_agui_body("s-livre", "bruno", "a2", "turno do bruno")))
        anonymous = await client.post("/agui/ag", json=_agui_body("s-livre", None, "a3"))
        third = await client.post("/agents/ag/runs", data=_form("s-livre", "carla"))
        calls = world.model_calls()

    assert owner_before == [None]
    assert [doc.get("user_id") for doc in session_docs(world.db)] == ["bruno"]
    for refused in (anonymous, third):
        assert (refused.status_code, refused.json()) == (404, NOT_FOUND)
    assert calls == 2
    runs = await stored_runs(world.db, "s-livre", user_id="bruno")
    assert [(run["run_id"], run.get("user_id")) for run in runs] == [("a1", None), ("a2", "bruno")]


# ── documento de sessão gravado pelo agno 2.5.8 (runs no campo ``runs`` da sessão) ───────────────


def _legacy_session() -> dict[str, Any]:
    """Sessão como o agno 2.5.8 a gravava (gerada com a venv 2.5.8 e o ``SessionDb`` da base): o run
    fica no campo ``runs`` do documento e não existe ``agno_runs``."""
    run = {
        "run_id": "r-legado",
        "agent_id": "ag",
        "agent_name": "ag",
        "session_id": "s-legado",
        "user_id": "ana",
        "content": HISTORY_MARK,
        "content_type": "str",
        "model": "fake-chat-model",
        "model_provider": "fake",
        "session_state": {},
        "created_at": 1794788538,
        "status": "COMPLETED",
        "metrics": {"time_to_first_token": 0.001, "duration": 0.002},
        "messages": [
            {"id": "m-u", "content": "guarde o codigo", "from_history": False, "stop_after_tool_call": False,
             "role": "user", "created_at": 1794788538},
            {"id": "m-a", "content": HISTORY_MARK, "from_history": False, "stop_after_tool_call": False,
             "role": "assistant", "created_at": 1794788538},
        ],
        "tools": [],
        "input": {"input_content": "guarde o codigo"},
    }
    return {
        "session_id": "s-legado",
        "session_type": "agent",
        "agent_id": "ag",
        "user_id": "ana",
        "runs": [run],
        "agent_data": {"name": "ag", "agent_id": "ag", "model": {"provider": "fake", "id": "fake-chat-model"}},
        "session_data": {"session_state": {}, "session_metrics": {}},
        "summary": None,
        "metadata": None,
        "created_at": 1794788538,
        "updated_at": 1794788538,
    }


async def test_sessao_gravada_pelo_2_5_8_segue_com_dono_e_com_historico(world: World) -> None:
    """Documento legado continua válido: o dono é respeitado antes de qualquer run no 3.x, a Ana segue
    a conversa vendo o histórico do campo ``runs`` e o run novo vai para ``agno_runs`` sem apagá-lo."""
    await world.db.get_sessions(user_id="ninguem")  # cria as coleções, como um processo já em uso
    await collection(world.db, world.db.session_table_name).insert_one(_legacy_session())

    async with world.client() as client:
        by_agui = await client.post("/agui/ag", json=_agui_body("s-legado", "bruno", "b1"))
        by_rest = await client.post("/agents/ag/runs", data=_form("s-legado", "bruno"))
        anonymous = await client.post("/agui/ag", json=_agui_body("s-legado", None, "b2"))
        calls_after_refusals = world.model_calls()
        # Pendência do F3-06 registrada no item: o run_id que só existe no campo legado não é visto
        # como "já gravado"; o do Bruno fica na sessão dele e o da Ana segue no histórico dela.
        reused = _finished(await client.post("/agui/ag", json=_agui_body("s-bruno", "bruno", "r-legado")))
        _finished(await client.post("/agui/ag", json=_agui_body("s-legado", "ana", "a2", "qual o codigo?")))

    for refused in (by_agui, by_rest, anonymous):
        assert (refused.status_code, refused.json()) == (404, NOT_FOUND)
        assert HISTORY_MARK not in refused.text
    assert calls_after_refusals == 0
    assert reused[0].run_id == "r-legado"
    seen_by_ana = [content for role, content in world.agent_model.calls[-1].messages if role != "system"]
    assert seen_by_ana == ["guarde o codigo", HISTORY_MARK, "qual o codigo?"]
    runs = await stored_runs(world.db, "s-legado", user_id="ana")
    assert [(run["run_id"], run["user_id"], run["content"]) for run in runs] == [
        ("r-legado", "ana", HISTORY_MARK),
        ("a2", "ana", "resposta"),
    ]
    assert [run["run_id"] for run in await stored_runs(world.db, "s-bruno", user_id="bruno")] == ["r-legado"]


# ── falha do Mongo na conferência do AG-UI ───────────────────────────────────────────────────────


@pytest.mark.parametrize("table", ["session_table_name", "runs_table_name"], ids=["dono-da-sessao", "run-ja-gravado"])
async def test_agui_com_mongo_fora_na_conferencia_responde_500_sem_o_texto_do_driver(
    world: World, table: str
) -> None:
    """Sem conseguir conferir o dono da sessão ou o ``runId``, o run não começa (fecha em vez de abrir)."""
    await world.db.get_sessions(user_id="ninguem")
    await world.db.get_run("nenhum")
    failing = FailingMongoCollection(ConnectionError(DRIVER_TEXT))
    collection(world.db, getattr(world.db, table)).find_one = failing.find_one  # type: ignore[method-assign]

    async with world.client() as client:
        response = await client.post("/agui/ag", json=_agui_body("s-ana", "ana", "r-ana"))

    assert response.status_code == 500
    assert "SENHA-DO-MONGO" not in response.text and "mongodb://" not in response.text
    assert "RUN_STARTED" not in response.text
    assert world.model_calls() == 0
    assert collection(world.db, world.db.runs_table_name).docs == []


# ── SessionDb.upsert_run: o que a recusa atômica pressupõe ───────────────────────────────────────


async def test_indice_unico_de_run_id_que_sustenta_a_recusa_e_criado_pelo_agno() -> None:
    """A recusa do ``run_id`` de outro dono é o ``DuplicateKeyError`` do índice único de ``run_id`` em
    ``agno_runs``, criado pelo agno (``agno/db/mongo/schemas.py``). Se um upgrade tirar o ``unique``,
    este teste falha antes de o run de outro dono virar um segundo documento com o mesmo ``run_id``."""
    db = fake_session_db()

    await db.upsert_run({"run_id": "r-1", "agent_id": "ag"}, "s-ana", user_id="ana")

    unique = [index["keys"] for index in collection(db, db.runs_table_name).indexes if index["unique"]]
    assert [("run_id", 1)] in unique


async def test_sem_o_indice_unico_o_run_do_dono_segue_intacto() -> None:
    """Falha de dependência documentada no ``upsert_run``: sem o índice (o agno só avisa quando não
    consegue criá-lo) o run de outro dono vira outro documento; o do dono não é trocado."""
    db = fake_session_db()
    await db.upsert_run({"run_id": "r-1", "agent_id": "ag", "content": "da ana"}, "s-ana", user_id="ana")
    runs = collection(db, db.runs_table_name)
    runs.unique_keys.clear()

    await db.upsert_run({"run_id": "r-1", "agent_id": "ag", "content": "do bruno"}, "s-bruno", user_id="bruno")

    by_owner = {(doc["session_id"], doc["user_id"]): doc["run_data"]["content"] for doc in runs.docs}
    assert by_owner == {("s-ana", "ana"): "da ana", ("s-bruno", "bruno"): "do bruno"}


@pytest.mark.parametrize(
    ("owner", "writer", "accepted"),
    [
        (None, "bruno", True),
        (None, None, True),
        ("ana", "ana", True),
        ("ana", None, False),
        ("ana", "bruno", False),
        ("ana", "", False),
        ("", None, False),
        ("ana", "Ana", False),
    ],
    ids=[
        "sem-dono-aceita-usuario",
        "sem-dono-aceita-anonimo",
        "proprio-dono",
        "anonimo-em-sessao-com-dono",
        "outro-usuario",
        "user-id-vazio",
        "dono-vazio-nao-e-sem-dono",
        "outra-caixa",
    ],
)
async def test_upsert_run_confere_o_dono_da_sessao_ja_gravada(
    owner: str | None, writer: str | None, accepted: bool
) -> None:
    db = fake_session_db()
    await db.get_sessions(user_id="ninguem")
    await collection(db, db.session_table_name).insert_one({"session_id": "s-1", "user_id": owner})

    if accepted:
        await db.upsert_run({"run_id": "r-novo", "agent_id": "ag"}, "s-1", user_id=writer)
    else:
        with pytest.raises(RunOwnershipError) as refused:
            await db.upsert_run({"run_id": "r-novo", "agent_id": "ag"}, "s-1", user_id=writer)
        assert "ana" not in str(refused.value) and "s-1" not in str(refused.value)

    stored = [(doc["run_id"], doc["user_id"]) for doc in collection(db, db.runs_table_name).docs]
    assert stored == ([("r-novo", writer)] if accepted else [])


async def test_documento_de_sessao_sem_o_campo_user_id_e_tratado_como_sem_dono() -> None:
    """Documento legado sem a chave ``user_id`` (e não ``user_id: null``): aceita, como o ``upsert_session``."""
    db = fake_session_db()
    await db.get_sessions(user_id="ninguem")
    await collection(db, db.session_table_name).insert_one({"session_id": "s-antiga"})

    await db.upsert_run({"run_id": "r-1", "agent_id": "ag"}, "s-antiga", user_id="ana")

    assert [doc["user_id"] for doc in collection(db, db.runs_table_name).docs] == ["ana"]


# ── rotas novas do 3.x (classe RUN) que leem ou copiam uma sessão ────────────────────────────────


async def test_rotas_novas_de_sessao_do_3x_nao_servem_a_sessao_da_ana_ao_user_id_do_bruno(world: World) -> None:
    """``fork``, ``compact``, ``continue`` e a leitura da sessão, com a chave run e o ``user_id`` de
    outro usuário: nada da Ana na resposta, nenhuma sessão nova e o histórico dela como estava."""
    async with world.client() as client:
        owner = await client.post("/agents/ag/runs", data=_form("s-ana", "ana", HISTORY_MARK))
        run_id = owner.json()["run_id"]
        before = await _snapshot(world.db, "s-ana", "ana")
        bruno = {"user_id": "bruno"}

        responses = {
            "fork": await client.post("/agents/ag/sessions/s-ana/fork", params=bruno),
            "compact": await client.post("/agents/ag/sessions/s-ana/compact", params=bruno),
            "continue": await client.post(
                f"/agents/ag/runs/{run_id}/continue", data=_form("s-ana", "bruno", tools="[]")
            ),
            "sessao": await client.get("/sessions/s-ana", params={**bruno, "type": "agent"}),
            "runs": await client.get("/sessions/s-ana/runs", params={**bruno, "type": "agent"}),
            "apagar": await client.delete("/sessions/s-ana", params={**bruno, "type": "agent"}),
        }

    statuses = {name: response.status_code for name, response in responses.items()}
    assert statuses == {"fork": 400, "compact": 404, "continue": 404, "sessao": 404, "runs": 404, "apagar": 403}
    assert all(HISTORY_MARK not in response.text for response in responses.values())
    assert await _snapshot(world.db, "s-ana", "ana") == before
    assert [doc["session_id"] for doc in session_docs(world.db)] == ["s-ana"]
    assert len(world.agent_model.calls) == 1


async def test_chave_run_sem_user_id_le_checkpoints_e_copia_a_sessao_registro_de_risco(world: World) -> None:
    """Caracterização, mesma classe do registro do F1-06 (``GET /sessions/{id}/runs`` sem ``user_id``):
    as rotas novas do 3.x ``checkpoints`` e ``fork`` são da classe RUN e não amarram o ``user_id`` à
    credencial. Quem tem a chave run e sabe o ``session_id`` (e o ``run_id``) lê os checkpoints e
    copia a sessão; a cópia continua sendo da Ana (o Bruno não a recebe pelo ``user_id`` dele).
    Identidade do principal: F5."""
    async with world.client() as client:
        owner = await client.post("/agents/ag/runs", data=_form("s-ana", "ana", HISTORY_MARK))
        run_id = owner.json()["run_id"]

        checkpoints = await client.get(f"/agents/ag/runs/{run_id}/checkpoints", params={"session_id": "s-ana"})
        fork = await client.post("/agents/ag/sessions/s-ana/fork")
        copied = fork.json()["session_id"]
        as_bruno = await client.get(f"/sessions/{copied}", params={"user_id": "bruno", "type": "agent"})

    assert checkpoints.status_code == 200 and checkpoints.json()["session_id"] == "s-ana"
    assert fork.status_code == 200 and copied != "s-ana"
    owners = {doc["session_id"]: doc["user_id"] for doc in session_docs(world.db)}
    assert owners == {"s-ana": "ana", copied: "ana"}
    assert as_bruno.status_code == 404


async def test_busca_de_sessoes_do_3x_com_a_chave_run_lista_as_de_todos_os_usuarios_registro_de_risco(
    world: World,
) -> None:
    """Caracterização, mesma classe do registro do F1-06/F5-02 (``GET /sessions`` sem ``user_id``): a
    rota nova ``POST /sessions/search`` é da classe RUN, escopa só pelo ``user_id`` do JWT do agno (que
    não usamos) e ignora o ``user_id`` do corpo e da query; o ``session_name`` é o texto do primeiro
    turno. Identidade do principal: F5-02. ``GET /sessions?user_id=`` segue filtrando."""
    async with world.client() as client:
        await client.post("/agents/ag/runs", data=_form("s-ana", "ana", HISTORY_MARK))
        await client.post("/agents/ag/runs", data=_form("s-bruno", "bruno", "turno do bruno"))

        as_bruno = await client.post("/sessions/search", params={"user_id": "bruno"}, json={"user_id": "bruno"})
        listed = await client.get("/sessions", params={"type": "agent", "user_id": "bruno"})

    assert as_bruno.status_code == 200
    assert sorted(item["session_id"] for item in as_bruno.json()["data"]) == ["s-ana", "s-bruno"]
    assert HISTORY_MARK in as_bruno.text
    assert [item["session_id"] for item in listed.json()["data"]] == ["s-bruno"]
