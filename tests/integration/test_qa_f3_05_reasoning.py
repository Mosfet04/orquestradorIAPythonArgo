"""QA do F3-05: ``reasoning=true`` no agno 3.1.12 pelo caminho de produção (fábrica real + app real).

O golden (``tests/golden/test_agno_kwargs_golden.py``) prova os kwargs. Aqui, o comportamento: o
modelo recebe ``think``/``analyze`` e as instruções de raciocínio só quando o documento pede, a
chamada de ``think`` roda no próprio run (sem fase separada) e aparece como tool no REST e como
``TOOL_CALL_*`` no AG-UI, com o run válido até o fim.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from ag_ui.core import EventType
from agno.agent import Agent
from agno.models.response import ModelResponse
from fastapi import FastAPI

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.tool import HttpMethod, ParameterType, Tool, ToolParameter
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from src.infrastructure.runtime.agno.session_db import SessionDb
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import FakeChatModel, FakeEmbedderFactory, FakeModelFactory, InMemoryToolRepository, RecordingLogger
from tests.fakes.agui import assert_valid_run, parse_agui_sse, text_of
from tests.fakes.session_db import fake_session_db, stored_runs
from tests.fakes.web import LOOPBACK_BASE_URL, LOOPBACK_CLIENT, mount_agent_os

THOUGHT = "PASSO-DE-RACIOCINIO-7421"
_ENV_NAMES = ("ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS", "APP_HOST", "API_KEY_RUN", "API_KEY_ADMIN")


def _think() -> ModelResponse:
    arguments = {"title": "plano", "thought": THOUGHT, "action": "responder", "confidence": 0.9}
    call = {"id": "call-1", "type": "function", "function": {"name": "think", "arguments": json.dumps(arguments)}}
    return ModelResponse(role="assistant", tool_calls=[call])


class _ScriptedFactory(FakeModelFactory):
    """Cada modelo criado recebe o roteiro ``[think, resposta final]``."""

    def create_model(self, config: Any) -> FakeChatModel:
        model = super().create_model(config)
        model.responses = [_think(), "resposta final"]
        return model


def _service(
    db: SessionDb, models: FakeModelFactory, tools: InMemoryToolRepository | None = None
) -> AgentFactoryService:
    logger = RecordingLogger()
    return AgentFactoryService(
        db_url="mongodb://mongo.test.invalid:27017",
        session_db=db,
        logger=logger,
        model_factory=models,
        embedder_factory=FakeEmbedderFactory(),
        tool_factory=HttpToolFactory(logger=logger),
        tool_repository=tools or InMemoryToolRepository(),
    )


def _config(agent_id: str, **options: Any) -> AgentConfig:
    return AgentConfig(
        id=agent_id, nome=agent_id, factory_ia_model="fake", model="m", descricao="d", prompt="p", **options
    )


def _app(monkeypatch: pytest.MonkeyPatch, db: SessionDb, agents: list[Agent]) -> FastAPI:
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    factory = AppFactory()
    app = factory.create_app()
    mount_agent_os(factory, app, agents, [], session_db=db)
    return app


def _client(app: FastAPI) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app, client=LOOPBACK_CLIENT)
    return httpx.AsyncClient(transport=transport, base_url=LOOPBACK_BASE_URL)


def _system(model: FakeChatModel) -> str:
    return "\n".join(content for role, content in model.calls[0].messages if role == "system")


async def test_so_o_agente_com_reasoning_recebe_as_tools_e_as_instrucoes_de_raciocinio() -> None:
    """Duas configs na mesma fábrica: a toolkit de raciocínio não vaza para o agente sem ``reasoning``
    (nem pelo default: documento legado sem o campo segue sem raciocínio)."""
    models = FakeModelFactory(responses=["ok"])
    service = _service(fake_session_db(), models)
    thinker = await service.create_agent(_config("pensa", reasoning=True))
    plain = await service.create_agent(_config("direto", reasoning=False))
    legacy = await service.create_agent(_config("legado"))

    for built in (thinker, plain, legacy):
        await built.arun("oi")
    with_reasoning, without, default = models.models

    assert sorted(with_reasoning.calls[0].tool_names) == ["analyze", "think"]
    assert "<reasoning_instructions>" in _system(with_reasoning)
    for model in (without, default):
        assert model.calls[0].tool_names == ()
        assert "reasoning_instructions" not in _system(model)


async def test_reasoning_se_soma_as_tools_do_documento_sem_substitui_las() -> None:
    cep = Tool(
        id="cep",
        name="cep",
        description="Busca endereço",
        route="https://api.example.invalid/cep/{cep}",
        http_method=HttpMethod.GET,
        parameters=[ToolParameter(name="cep", type=ParameterType.STRING, description="CEP", required=True)],
    )
    tools = InMemoryToolRepository([cep])
    models = FakeModelFactory(responses=["ok"])
    service = _service(fake_session_db(), models, tools)
    both = await service.create_agent(_config("pensa-e-busca", reasoning=True, tools_ids=["cep"]))
    only_tool = await service.create_agent(_config("so-busca", tools_ids=["cep"]))

    await both.arun("oi")
    await only_tool.arun("oi")

    assert sorted(models.models[0].calls[0].tool_names) == ["analyze", "cep", "think"]
    assert models.models[1].calls[0].tool_names == ("cep",)


async def test_think_roda_no_proprio_run_e_aparece_como_tool_no_rest(monkeypatch: pytest.MonkeyPatch) -> None:
    """Sem fase separada: duas chamadas ao modelo (a que pede ``think`` e a da resposta) num run só."""
    db = fake_session_db()
    models = _ScriptedFactory()
    app = _app(monkeypatch, db, [await _service(db, models).create_agent(_config("pensa", reasoning=True))])

    async with _client(app) as client:
        response = await client.post(
            "/agents/pensa/runs", data={"message": "oi", "stream": "false", "user_id": "ana", "session_id": "s-1"}
        )

    body = response.json()
    assert (response.status_code, body["status"], body["content"]) == (200, "COMPLETED", "resposta final")
    [call] = body["tools"]
    assert (call["tool_name"], call["tool_args"]["thought"], call["tool_call_error"]) == ("think", THOUGHT, False)
    [model] = models.models
    assert len(model.calls) == 2 and not model.exhausted
    assert [run["status"] for run in await stored_runs(db, "s-1", user_id="ana")] == ["COMPLETED"]  # um run só


async def test_think_no_agui_sai_como_tool_call_e_o_run_termina_valido(monkeypatch: pytest.MonkeyPatch) -> None:
    """Diferença registrada no item: o raciocínio chega ao cliente AG-UI como ``TOOL_CALL_*`` (com o
    texto nos argumentos); nenhum ``REASONING_*``/``STEP_*`` e o terminal é ``RUN_FINISHED``."""
    db = fake_session_db()
    app = _app(monkeypatch, db, [await _service(db, _ScriptedFactory()).create_agent(_config("pensa", reasoning=True))])
    body = {
        "threadId": "t-1",
        "runId": "r-1",
        "state": {},
        "messages": [{"id": "m1", "role": "user", "content": "oi"}],
        "tools": [],
        "context": [],
        "forwardedProps": {"user_id": "ana"},
    }

    async with _client(app) as client:
        response = await client.post("/agui/pensa", json=body)

    events = parse_agui_sse(response.text)
    assert_valid_run(events)
    types = [event.type for event in events]
    assert types[-1] == EventType.RUN_FINISHED and text_of(events) == "resposta final"
    [started] = [event for event in events if event.type == EventType.TOOL_CALL_START]
    assert started.tool_call_name == "think"
    [arguments] = [event.delta for event in events if event.type == EventType.TOOL_CALL_ARGS]
    assert json.loads(arguments)["thought"] == THOUGHT
    assert types.count(EventType.TOOL_CALL_RESULT) == 1
    assert not [kind for kind in types if kind.value.startswith(("REASONING", "STEP", "STATE", "RAW", "CUSTOM"))]
