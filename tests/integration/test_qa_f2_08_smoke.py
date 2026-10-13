"""QA do F2-08: smoke do app real com o db de sessões compartilhado.

Pilha: ``AppFactory`` (lifespan) -> ``DependencyContainer`` (config em YAML) -> controller -> use cases ->
``AgnoRuntime`` -> AgentOS (``db=`` o ``SessionDb``), sobre ``tests/fakes/startup_world.py`` (Mongo, cliente
motor do db de sessões e modelo falsos), em processo (``TestClient``, sem bind de porta, sem rede). Uma
fiação só: startup, run REST com ``user_id`` e ``session_id``, segundo run na mesma sessão vendo o
histórico, ``/sessions`` e ``/memories`` com a chave admin, rotas ``/schedules`` e ``/approvals`` sob
admin, e o shutdown (um cliente de sessões fechado uma vez, fora do loop). Chaves são valores de teste.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from httpx import Response
from starlette.testclient import TestClient

from src.infrastructure import dependency_injection as di
from src.infrastructure.runtime.agno.session_db import SessionDb
from src.infrastructure.web import app_factory
from tests.fakes import FakeChatModel
from tests.fakes.session_db import session_db_client, session_docs
from tests.fakes.startup_world import CLEAN_ENV, Events, startup_world
from tests.fakes.web import LOOPBACK_BASE_URL, LOOPBACK_CLIENT

RUN_KEY = "qa8-run-key-" + "r" * 22
ADMIN_KEY = "qa8-admin-key-" + "a" * 20
RUN = {"Authorization": f"Bearer {RUN_KEY}"}
ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}

CONFIG = """
agents:
  - id: ag
    nome: Agente do YAML
    factoryIaModel: ollama
    model: m-ag
    descricao: d
    prompt: [Você é um agente de teste.]
    active: true
teams:
  - id: tm
    nome: Time do YAML
    factoryIaModel: ollama
    model: mt-tm
    prompt: Coordena.
    member_ids: [ag]
    mode: coordinate
    active: true
"""
FIRST = "meu código de cliente é 4815"
SECOND = "qual é o meu código?"


class _World:
    def __init__(self, http: TestClient, events: Events, models: dict[str, FakeChatModel]) -> None:
        self.http = http
        self.events = events
        self.models = models
        self.session_dbs: list[SessionDb] = []


@contextmanager
def smoke_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[_World]:
    for name in CLEAN_ENV:
        monkeypatch.delenv(name, raising=False)
    path = tmp_path / "config.yaml"
    path.write_text(CONFIG, encoding="utf-8")
    for name, value in {
        "AGNO_TELEMETRY": "false",
        "OTEL_ENABLED": "false",
        "CONFIG_STORE": "yaml",
        "CONFIG_YAML_PATH": str(path),
        "ENVIRONMENT": "production",
        "APP_HOST": "127.0.0.1",
        "API_KEY_RUN": RUN_KEY,
        "API_KEY_ADMIN": ADMIN_KEY,
    }.items():
        monkeypatch.setenv(name, value)

    models: dict[str, FakeChatModel] = {
        "m-ag": FakeChatModel(id="m-ag", responses=["r1", "r2", "r3"]),
        "mt-tm": FakeChatModel(id="mt-tm", responses=["do time"] * 3),
    }

    def create_model(self: Any, config: Any) -> FakeChatModel:
        return models[config.model_id]

    events = Events()
    created: list[SessionDb] = []
    with startup_world(events, agents=()):
        original_create = di.create_session_db

        def spy_create(db_url: str, db_name: str) -> SessionDb:
            db = original_create(db_url, db_name)
            created.append(db)
            return db

        with patch.object(di.ProviderRegistry, "create_model", create_model), patch.object(
            di, "create_session_db", spy_create
        ):
            app = app_factory.AppFactory().create_app()
            with TestClient(
                app, base_url=LOOPBACK_BASE_URL, client=LOOPBACK_CLIENT, raise_server_exceptions=False
            ) as http:
                world = _World(http, events, models)
                world.session_dbs = created
                yield world


def test_startup_run_com_user_e_sessao_historico_no_segundo_run_sessions_memories_e_shutdown(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with smoke_app(monkeypatch, tmp_path) as world:
        responses = _runs_sessions_and_memories(world.http)
        db = world.session_dbs[0]
        during_closes = list(session_db_client(db).closes)

    _assert_history_per_user(world, responses)
    _assert_sessions_and_memories_per_user(responses, db)

    # Um db de sessões para o app inteiro; fechado no shutdown, uma vez e numa thread.
    assert len(world.session_dbs) == 1 and during_closes == []
    assert session_db_client(db).closes == [False]
    assert world.events.count("db:created") == 1 and world.events.count("db:close") == 1
    assert not [e for e in world.events.names if e.endswith(":on-loop") or e == "db:use-after-close"]


def _runs_sessions_and_memories(http: TestClient) -> dict[str, Response]:
    """Dois runs da Ana na mesma sessão, um do Bruno com o mesmo ``session_id``, um de team e as leituras admin."""

    def run(path: str, message: str, user_id: str, session_id: str) -> Response:
        data = {"message": message, "stream": "false", "user_id": user_id, "session_id": session_id}
        return http.post(path, data=data, headers=RUN)

    return {
        "listed": http.get("/agents", headers=RUN),
        "first": run("/agents/ag/runs", FIRST, "ana", "s1"),
        "second": run("/agents/ag/runs", SECOND, "ana", "s1"),
        "other_user": run("/agents/ag/runs", SECOND, "bruno", "s1"),
        "team": run("/teams/tm/runs", "oi", "ana", "s-team"),
        "sessions": http.get("/sessions", params={"type": "agent", "user_id": "ana"}, headers=ADMIN),
        "team_sessions": http.get("/sessions", params={"type": "team", "user_id": "ana"}, headers=ADMIN),
        "session_runs": http.get("/sessions/s1/runs", params={"type": "agent", "user_id": "ana"}, headers=ADMIN),
        "created": http.post("/memories", json={"memory": "Ana prefere espanhol", "user_id": "ana"}, headers=ADMIN),
        "memories": http.get("/memories", params={"user_id": "ana"}, headers=ADMIN),
        "bruno_memories": http.get("/memories", params={"user_id": "bruno"}, headers=ADMIN),
    }


def _assert_history_per_user(world: _World, responses: dict[str, Response]) -> None:
    """O segundo run da Ana vê o histórico do primeiro; o Bruno, no mesmo ``session_id``, não."""
    first, second, team = responses["first"], responses["second"], responses["team"]
    assert sorted(a["id"] for a in responses["listed"].json()) == ["ag"]
    assert (first.status_code, second.status_code, team.status_code) == (200, 200, 200)
    assert (first.json()["content"], second.json()["content"]) == ("r1", "r2")
    prompts = [call.messages for call in world.models["m-ag"].calls]
    texts = ["\n".join(content for _, content in call) for call in prompts]
    assert texts[0].count(FIRST) == 1  # o primeiro run só tem a própria mensagem
    assert texts[1].count(FIRST) == 1 and "r1" in texts[1]  # o segundo run viu o histórico (pergunta e resposta)
    # Bruno usou o mesmo session_id e não viu o histórico da Ana (a sessão dela segue dela).
    assert responses["other_user"].status_code == 200
    assert FIRST not in texts[2] and "r1" not in texts[2]


def _assert_sessions_and_memories_per_user(responses: dict[str, Response], db: SessionDb) -> None:
    """Rotas admin de sessões e memórias por ``user_id``; os documentos gravados são os da Ana."""
    assert [s["session_id"] for s in responses["sessions"].json()["data"]] == ["s1"]
    assert [s["session_id"] for s in responses["team_sessions"].json()["data"]] == ["s-team"]
    assert len(responses["session_runs"].json()) == 2
    created = responses["created"]
    assert created.status_code == 200, created.text
    assert [m["memory"] for m in responses["memories"].json()["data"]] == ["Ana prefere espanhol"]
    assert responses["bruno_memories"].json()["data"] == []
    assert sorted((d["session_id"], d["user_id"]) for d in session_docs(db)) == [("s-team", "ana"), ("s1", "ana")]


@pytest.mark.parametrize(
    ("method", "path"),
    [
        ("GET", "/schedules"),
        ("POST", "/schedules"),
        ("GET", "/schedules/x"),
        ("POST", "/schedules/x/trigger"),
        ("GET", "/approvals"),
        ("GET", "/approvals/count"),
        ("POST", "/approvals/x/resolve"),
        ("GET", "/components"),
    ],
)
def test_rotas_de_agenda_aprovacao_e_componentes_so_com_a_chave_admin(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, method: str, path: str
) -> None:
    with smoke_app(monkeypatch, tmp_path) as world:
        no_key = world.http.request(method, path, json={})
        wrong_key = world.http.request(method, path, json={}, headers={"Authorization": "Bearer " + "x" * 32})
        as_run = world.http.request(method, path, json={}, headers=RUN)
        as_admin = world.http.request(method, path, json={}, headers=ADMIN)

    assert no_key.status_code == 401
    assert wrong_key.status_code == 401
    assert as_run.status_code == 403
    assert as_admin.status_code not in (401, 403), (method, path, as_admin.text[:200])


def test_agente_so_no_banco_de_componentes_nao_e_servido_pelo_app_real(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with smoke_app(monkeypatch, tmp_path) as world:
        db = world.session_dbs[0]
        database = session_db_client(db)[db.db_name]
        database["agno_components"].docs.append(
            {"component_id": "fantasma", "component_type": "agent", "name": "fantasma", "current_version": 1}
        )
        database["agno_component_configs"].docs.append(
            {"component_id": "fantasma", "version": 1, "stage": "published", "config": {"id": "fantasma"}}
        )
        listed = world.http.get("/agents", headers=RUN)
        detail = world.http.get("/agents/fantasma", headers=RUN)
        run = world.http.post("/agents/fantasma/runs", data={"message": "oi", "stream": "false"}, headers=RUN)

    assert [a["id"] for a in listed.json()] == ["ag"]
    assert (detail.status_code, run.status_code) == (404, 404)
