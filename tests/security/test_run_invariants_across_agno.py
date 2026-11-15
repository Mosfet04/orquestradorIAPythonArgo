"""F3-04: invariantes da rota de run e do AG-UI que valem no agno 2.5.8 e no 3.x.

O contrato muda entre as linhas do agno (decisões do dono; runbook no F3-07): com o ``session_id``
de outro usuário, o 2.5.8 roda o run numa conversa à parte (200) e o 3.x o recusa (404, pelo
``user_scope``); ``background=true`` sem ``stream=false`` vira SSE no 3.x. Estes testes não fixam o
que muda: fixam o que não pode mudar. O modelo ecoa o que recebeu, então o que ele viu aparece na
resposta HTTP (sem depender de qual cópia do agente o AgentOS usou).
"""

from __future__ import annotations

import asyncio
import copy
import json
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from ag_ui.core import EventType
from agno.agent import Agent
from agno.models.response import ModelResponse
from fastapi import FastAPI

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.model_config import ModelConfig
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from src.infrastructure.runtime.agno.session_db import SessionDb
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import FakeChatModel, FakeEmbedderFactory, FakeModelFactory, InMemoryToolRepository, RecordingLogger
from tests.fakes.agui import assert_valid_run, parse_agui_sse, text_of
from tests.fakes.session_db import fake_session_db, session_docs, stored_runs
from tests.fakes.web import mount_agent_os

RUN_KEY = "f304-run-key-" + "r" * 22
ADMIN_KEY = "f304-admin-key-" + "a" * 20
RUN = {"Authorization": f"Bearer {RUN_KEY}"}
_ENV_NAMES = ("ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS", "APP_HOST", "API_KEY_RUN", "API_KEY_ADMIN")
HISTORY_MARK = "o cofre da Ana abre com 7421"
QUESTION = "qual é o código do cofre?"


@dataclass
class EchoModel(FakeChatModel):
    """Responde com as mensagens que recebeu (menos as de sistema), sem roteiro."""

    def _next_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        call = self._as_call(args, kwargs)
        self.calls.append(call)
        seen = " | ".join(f"{role}: {content}" for role, content in call.messages if role != "system")
        return ModelResponse(role="assistant", content=f"[viu] {seen}")


class EchoModelFactory(FakeModelFactory):
    def create_model(self, config: ModelConfig) -> FakeChatModel:
        return EchoModel(id=config.model_id, provider=config.provider)


async def _agent(db: SessionDb) -> Agent:
    """Agente montado pela fábrica de produção (histórico ligado por default, db de sessões)."""
    logger = RecordingLogger()
    service = AgentFactoryService(
        db_url="mongodb://mongo.test.invalid:27017",
        session_db=db,
        logger=logger,
        model_factory=EchoModelFactory(),
        embedder_factory=FakeEmbedderFactory(),
        tool_factory=HttpToolFactory(logger=logger),
        tool_repository=InMemoryToolRepository(),
    )
    return await service.create_agent(
        AgentConfig(id="ag", nome="ag", factory_ia_model="fake", model="m", descricao="d", prompt="p")
    )


@pytest.fixture
def session_db() -> SessionDb:
    return fake_session_db()


@pytest.fixture
async def app(monkeypatch: pytest.MonkeyPatch, session_db: SessionDb) -> FastAPI:
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    monkeypatch.setenv("APP_HOST", "0.0.0" + ".0")
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("API_KEY_RUN", RUN_KEY)
    monkeypatch.setenv("API_KEY_ADMIN", ADMIN_KEY)
    factory = AppFactory()
    application = factory.create_app()
    mount_agent_os(factory, application, [await _agent(session_db)], [], session_db=session_db)
    return application


def _client(app: FastAPI) -> httpx.AsyncClient:
    return httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test", headers=RUN)


async def _rest_run(client: httpx.AsyncClient, message: str, user_id: str, session_id: str) -> httpx.Response:
    return await client.post(
        "/agents/ag/runs", data={"message": message, "user_id": user_id, "session_id": session_id, "stream": "false"}
    )


async def _agui_run(client: httpx.AsyncClient, message: str, user_id: str, thread_id: str) -> httpx.Response:
    body = {
        "threadId": thread_id,
        "runId": f"r-{user_id}",
        "state": {},
        "messages": [{"id": "m1", "role": "user", "content": message}],
        "tools": [],
        "context": [],
        "forwardedProps": {"user_id": user_id},
    }
    return await client.post("/agui/ag", json=body)


def _agui_text(response: httpx.Response) -> str:
    events = parse_agui_sse(response.text)
    assert_valid_run(events)
    # RUN_ERROR também é terminal válido: sem esta checagem um run que falha em silêncio passaria.
    assert events[-1].type == EventType.RUN_FINISHED, response.text
    return text_of(events)


async def _ana_state(db: SessionDb) -> tuple[list[dict[str, Any]], list[str]]:
    """Documento da sessão da Ana e as entradas dos runs gravados nela (API pública do db)."""
    docs = [d for d in session_docs(db) if d["session_id"] == "s-ana"]
    runs = await stored_runs(db, "s-ana", user_id="ana")
    return copy.deepcopy(docs), [json.dumps(r.get("input"), ensure_ascii=False) for r in runs]


@pytest.mark.parametrize("intruder_via", ["rest", "agui"])
async def test_sessao_de_outro_user_id_nunca_e_lida_nem_sobrescrita(
    app: FastAPI, session_db: SessionDb, intruder_via: str
) -> None:
    """Bruno manda o ``session_id`` da Ana: não vê o histórico dela e nada dele entra na sessão dela."""
    async with _client(app) as client:
        first = await _rest_run(client, HISTORY_MARK, "ana", "s-ana")
        assert first.status_code == 200 and first.json()["status"] == "COMPLETED", first.text
        before = await _ana_state(session_db)

        if intruder_via == "rest":
            intruder = await _rest_run(client, QUESTION, "bruno", "s-ana")
            # 2.5.8: conversa à parte (200); 3.x: o user_scope recusa a sessão de outro dono (404).
            assert intruder.status_code in (200, 404), intruder.text
            seen_by_bruno = intruder.text
            if intruder.status_code == 200:
                # O run do Bruno rodou de fato e ecoou o que recebeu: senão o "não viu" abaixo é vazio.
                assert intruder.json()["status"] == "COMPLETED", intruder.text
                assert QUESTION in intruder.json()["content"], intruder.text
        else:
            intruder = await _agui_run(client, QUESTION, "bruno", "s-ana")
            assert intruder.status_code == 200, intruder.text
            seen_by_bruno = _agui_text(intruder)
            assert QUESTION in seen_by_bruno, intruder.text
        after = await _ana_state(session_db)
        # Controle positivo: o histórico está ligado e a Ana segue vendo o dela, sem o turno do Bruno.
        again = await _rest_run(client, "e o código?", "ana", "s-ana")

    assert HISTORY_MARK not in seen_by_bruno
    assert after == before
    assert len(after[1]) == 1 and HISTORY_MARK in after[1][0]
    assert again.status_code == 200, again.text
    seen_by_ana = again.json()["content"]
    assert HISTORY_MARK in seen_by_ana
    assert QUESTION not in seen_by_ana


async def test_background_com_stream_false_responde_202_em_json_e_o_run_conclui(app: FastAPI) -> None:
    """``background=true`` + ``stream=false``: o mesmo contrato no 2.5.8 e no 3.x (sem ``stream=false``,
    o 3.x responde SSE)."""
    async with _client(app) as client:
        started = await client.post(
            "/agents/ag/runs",
            data={"message": "trabalho em segundo plano", "user_id": "ana", "background": "true", "stream": "false"},
        )
        assert started.status_code == 202, started.text
        assert started.headers["content-type"].startswith("application/json")
        body = started.json()
        assert set(body) == {"run_id", "session_id", "status"}
        assert body["run_id"] and body["session_id"]

        polled: dict[str, Any] = {}
        for _ in range(500):
            response = await client.get(f"/agents/ag/runs/{body['run_id']}", params={"session_id": body["session_id"]})
            if response.status_code == 200 and response.json().get("status") == "COMPLETED":
                polled = response.json()
                break
            await asyncio.sleep(0.01)

    assert polled, "o run de background não concluiu"
    assert polled["run_id"] == body["run_id"]
    assert "trabalho em segundo plano" in polled["content"]
