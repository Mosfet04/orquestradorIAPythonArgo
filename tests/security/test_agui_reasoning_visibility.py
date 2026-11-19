"""F3-10: ``expose_reasoning=false`` tira do AG-UI o texto do raciocínio do agente (decisão do dono, 2026-11-16).

Pelo caminho de produção (``AgentFactoryService``/``TeamFactoryService`` reais, app real, AG-UI com o
conversor do agno 3.1.12) e modelo scriptado: o agente chama ``think``/``analyze`` e responde. Com o campo
desligado, nenhum evento do stream leva o texto do raciocínio e a resposta chega igual; o run segue com o
raciocínio, gravado na sessão. Com o default (documento sem o campo), os ``TOOL_CALL_*`` de raciocínio
chegam (F3-05). Tool do usuário chamada ``think`` não é confundida com a de raciocínio, e em Team vale
a configuração de cada membro.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from ag_ui.core import BaseEvent, EventType, ToolCallStartEvent
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.team import Team
from agno.tools.function import Function
from fastapi import FastAPI

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.model_config import ModelConfig
from src.domain.entities.team_config import TeamConfig
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from src.infrastructure.runtime.agno.reasoning_visibility import PrivateReasoningTools
from src.infrastructure.runtime.agno.session_db import SessionDb
from src.infrastructure.runtime.agno.team_factory_service import TeamFactoryService
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import FakeChatModel, FakeEmbedderFactory, FakeModelFactory, InMemoryToolRepository, RecordingLogger
from tests.fakes.agui import assert_valid_run, parse_agui_sse, text_of
from tests.fakes.session_db import fake_session_db, stored_runs
from tests.fakes.web import LOOPBACK_BASE_URL, LOOPBACK_CLIENT, mount_agent_os

THOUGHT = "PENSAMENTO-PRIVADO-8813"
ANALYSIS = "ANALISE-PRIVADA-2207"
PUBLIC_THOUGHT = "PENSAMENTO-PUBLICO-4410"
USER_TOOL_TEXT = "TOOL-DO-USUARIO-3391"
_ENV_NAMES = ("ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS", "APP_HOST", "API_KEY_RUN", "API_KEY_ADMIN")
_TOOL_CALL_TYPES = (
    EventType.TOOL_CALL_START,
    EventType.TOOL_CALL_ARGS,
    EventType.TOOL_CALL_END,
    EventType.TOOL_CALL_RESULT,
)


def _calls(*calls: tuple[str, str, dict[str, Any]]) -> ModelResponse:
    """Uma resposta do modelo com as tool calls ``(id, nome, argumentos)``."""
    return ModelResponse(
        role="assistant",
        tool_calls=[
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
            for call_id, name, arguments in calls
        ],
    )


def _think(call_id: str, thought: str) -> tuple[str, str, dict[str, Any]]:
    return call_id, "think", {"title": "plano", "thought": thought, "action": "responder", "confidence": 0.9}


def _analyze(call_id: str, analysis: str) -> tuple[str, str, dict[str, Any]]:
    return call_id, "analyze", {"title": "conferência", "result": "ok", "analysis": analysis, "confidence": 0.9}


def _reasoning_script(prefix: str, thought: str, answer: str) -> list[str | ModelResponse]:
    return [_calls(_think(f"{prefix}-think", thought)), _calls(_analyze(f"{prefix}-analyze", ANALYSIS)), answer]


class _ScriptedFactory(FakeModelFactory):
    """Roteiro por ``model`` da config (cada agente/team tem o seu)."""

    def __init__(self, scripts: dict[str, list[str | ModelResponse]]) -> None:
        super().__init__()
        self._scripts = scripts

    def create_model(self, config: ModelConfig) -> FakeChatModel:
        model = super().create_model(config)
        model.responses = list(self._scripts[config.model_id])
        return model


def _agents(db: SessionDb, models: FakeModelFactory) -> AgentFactoryService:
    logger = RecordingLogger()
    return AgentFactoryService(
        db_url="mongodb://mongo.test.invalid:27017",
        session_db=db,
        logger=logger,
        model_factory=models,
        embedder_factory=FakeEmbedderFactory(),
        tool_factory=HttpToolFactory(logger=logger),
        tool_repository=InMemoryToolRepository(),
    )


def _config(agent_id: str, **options: Any) -> AgentConfig:
    return AgentConfig(
        id=agent_id, nome=agent_id, factory_ia_model="fake", model=f"m-{agent_id}", descricao="d", prompt="p",
        reasoning=True, **options,
    )


def _app(monkeypatch: pytest.MonkeyPatch, db: SessionDb, agents: list[Agent], teams: list[Team]) -> FastAPI:
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    factory = AppFactory()
    app = factory.create_app()
    mount_agent_os(factory, app, agents, teams, session_db=db)
    return app


async def _agui(app: FastAPI, entity_id: str, thread_id: str) -> tuple[str, list[BaseEvent]]:
    body = {
        "threadId": thread_id,
        "runId": f"r-{thread_id}",
        "state": {},
        "messages": [{"id": "m1", "role": "user", "content": "oi"}],
        "tools": [],
        "context": [],
        "forwardedProps": {"user_id": "ana"},
    }
    transport = httpx.ASGITransport(app=app, client=LOOPBACK_CLIENT)
    async with httpx.AsyncClient(transport=transport, base_url=LOOPBACK_BASE_URL) as client:
        response = await client.post(f"/agui/{entity_id}", json=body)
    assert response.status_code == 200
    events = parse_agui_sse(response.text)
    assert_valid_run(events)
    assert events[-1].type == EventType.RUN_FINISHED, response.text
    return response.text, events


def _tool_names(events: list[BaseEvent]) -> list[str]:
    return [event.tool_call_name for event in events if isinstance(event, ToolCallStartEvent)]


def _tool_call_ids(events: list[BaseEvent]) -> set[str]:
    return {getattr(event, "tool_call_id", "") for event in events if event.type in _TOOL_CALL_TYPES}


async def test_campo_desligado_nao_emite_o_raciocinio_e_a_resposta_chega_igual(monkeypatch: pytest.MonkeyPatch):
    db = fake_session_db()
    models = _ScriptedFactory(
        {
            "m-privado": _reasoning_script("p", THOUGHT, "resposta final"),
            "m-exposto": _reasoning_script("e", THOUGHT, "resposta final"),
        }
    )
    service = _agents(db, models)
    private = await service.create_agent(_config("privado", expose_reasoning=False))
    exposed = await service.create_agent(_config("exposto"))  # documento sem o campo: default true
    app = _app(monkeypatch, db, [private, exposed], [])

    raw, events = await _agui(app, "privado", "t-privado")
    _, reference = await _agui(app, "exposto", "t-exposto")

    for secret in (THOUGHT, ANALYSIS):
        assert secret not in raw  # nenhum evento, de nenhum tipo, leva o texto do raciocínio
    assert not [event for event in events if event.type in _TOOL_CALL_TYPES]
    assert text_of(events) == text_of(reference) == "resposta final"
    assert _tool_names(reference) == ["think", "analyze"]  # o default é o do F3-05
    # O raciocínio rodou no run (três chamadas ao modelo) e ficou gravado na sessão.
    assert [len(model.calls) for model in models.models] == [3, 3]
    [run] = await stored_runs(db, "t-privado", user_id="ana")
    assert [(t["tool_name"], t["tool_args"].get("thought")) for t in run["tools"]] == [
        ("think", THOUGHT),
        ("analyze", None),
    ]


@pytest.mark.parametrize("options", [{}, {"expose_reasoning": True}], ids=["sem-o-campo", "true"])
async def test_com_o_default_os_tool_call_de_think_e_analyze_chegam(
    monkeypatch: pytest.MonkeyPatch, options: dict[str, Any]
):
    db = fake_session_db()
    models = _ScriptedFactory({"m-pensa": _reasoning_script("c", THOUGHT, "resposta final")})
    agent = await _agents(db, models).create_agent(_config("pensa", **options))

    raw, events = await _agui(_app(monkeypatch, db, [agent], []), "pensa", "t-1")

    assert _tool_names(events) == ["think", "analyze"]
    assert [t for t in _TOOL_CALL_TYPES if t not in {e.type for e in events}] == []
    assert THOUGHT in raw and ANALYSIS in raw
    assert text_of(events) == "resposta final"


def _user_tool(name: str) -> Function:
    async def run(text: str) -> str:
        return f"{USER_TOOL_TEXT}:{text}"

    return Function(name=name, description="Tool do usuário", entrypoint=run)


async def test_tool_do_usuario_chamada_think_e_as_demais_tools_nao_sao_afetadas(monkeypatch: pytest.MonkeyPatch):
    """A ``think`` do usuário vem antes da toolkit (como as tools do documento na fábrica): é ela que roda
    (o agno pula o nome repetido) e segue visível; a ``busca`` também; só a ``analyze`` é do raciocínio."""
    db = fake_session_db()
    model = FakeChatModel(
        responses=[
            _calls(("u-think", "think", {"text": "a"}), ("u-busca", "busca", {"text": "b"})),
            _calls(_analyze("r-analyze", ANALYSIS)),
            "resposta final",
        ]
    )
    agent = Agent(
        id="misto",
        name="misto",
        model=model,
        db=db,
        tools=[_user_tool("think"), _user_tool("busca"), PrivateReasoningTools(add_instructions=True)],
        telemetry=False,
    )

    raw, events = await _agui(_app(monkeypatch, db, [agent], []), "misto", "t-1")

    assert _tool_names(events) == ["think", "busca"]
    assert _tool_call_ids(events) == {"u-think", "u-busca"}
    assert raw.count(USER_TOOL_TEXT) == 2  # os resultados das tools do usuário chegam
    assert ANALYSIS not in raw
    assert text_of(events) == "resposta final"


async def test_em_team_vale_a_configuracao_de_cada_membro(monkeypatch: pytest.MonkeyPatch):
    db = fake_session_db()
    models = _ScriptedFactory(
        {
            "m-privado": _reasoning_script("p", THOUGHT, "resposta do privado"),
            "m-publico": [_calls(_think("u-think", PUBLIC_THOUGHT)), "resposta do publico"],
            "m-time": [
                _calls(("t-1", "delegate_task_to_member", {"member_id": "privado", "task": "pense"})),
                _calls(("t-2", "delegate_task_to_member", {"member_id": "publico", "task": "pense"})),
                "síntese do time",
            ],
        }
    )
    service = _agents(db, models)
    private = await service.create_agent(_config("privado", expose_reasoning=False))
    public = await service.create_agent(_config("publico"))
    team_config = TeamConfig(
        id="time", nome="time", factory_ia_model="fake", model="m-time", member_ids=["privado", "publico"],
        mode="coordinate", user_memory_active=False,
    )
    team = TeamFactoryService(session_db=db, logger=RecordingLogger(), model_factory=models).create_team(
        team_config, [private, public]
    )

    raw, events = await _agui(_app(monkeypatch, db, [private, public], [team]), "time", "t-time")

    for secret in (THOUGHT, ANALYSIS):
        assert secret not in raw
    assert PUBLIC_THOUGHT in raw  # o membro com o default mostra o raciocínio
    assert "think" in _tool_names(events) and "analyze" not in _tool_names(events)
    assert "u-think" in _tool_call_ids(events) and not {"p-think", "p-analyze"} & _tool_call_ids(events)
    assert "síntese do time" in text_of(events)
    assert [len(model.calls) for model in models.models] == [3, 2, 3]  # os dois membros rodaram até o fim
