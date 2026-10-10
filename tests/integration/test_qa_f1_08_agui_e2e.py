"""QA do F1-08: AG-UI por entidade de ponta a ponta (app real + chave run).

Pilha: ``AppFactory.create_app`` (CORS + auth por chave) -> ``mount_agent_os`` -> ``POST
/agui/{id}``. Modelo = ``FakeChatModel`` roteirizado; tools HTTP com ``httpx.MockTransport``
(nada sai da máquina). Todo stream é parseado com os modelos de ``ag_ui.core`` e confere as
regras de sequência do protocolo (``tests/fakes/agui.py``). Chaves são valores de teste.
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
import uvicorn
from ag_ui.core import (
    RunErrorEvent,
    RunFinishedEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.team import Team
from agno.team.mode import TeamMode
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.model_config import ModelConfig
from src.domain.entities.tool import HttpMethod, ParameterType, Tool, ToolParameter
from src.domain.ports import IModelFactory
from src.infrastructure.runtime.agno import agent_factory_service
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import FakeChatModel, FakeEmbedderFactory, InMemoryToolRepository, RecordingLogger
from tests.fakes.agui import assert_valid_run, parse_agui_sse, text_of, types_of
from tests.fakes.web import agno_runtime, mount_agent_os

RUN_KEY = "qa8-run-key-" + "r" * 21
ADMIN_KEY = "qa8-admin-key-" + "a" * 19
RUN = {"Authorization": f"Bearer {RUN_KEY}"}
ALL_INTERFACES = "0.0.0" + ".0"
_ENV_NAMES = ("ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS", "APP_HOST", "API_KEY_RUN", "API_KEY_ADMIN")
BASE = "https://api.example.invalid"

CEP = Tool(
    id="cep",
    name="CEP",
    description="Busca endereço pelo CEP",
    route=f"{BASE}/cep/{{cep}}",
    http_method=HttpMethod.GET,
    parameters=[ToolParameter(name="cep", type=ParameterType.STRING, description="CEP", required=True)],
)


def _tool_call(
    name: str, arguments: dict[str, Any], call_id: str = "call-1", content: str | None = None
) -> ModelResponse:
    return ModelResponse(
        role="assistant",
        content=content,
        tool_calls=[
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
        ],
    )


def _body(
    messages: list[dict[str, Any]] | None = None,
    thread_id: str | None = None,
    run_id: str | None = None,
    forwarded: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "threadId": thread_id or f"t-{uuid.uuid4().hex[:8]}",
        "runId": run_id or f"r-{uuid.uuid4().hex[:8]}",
        "state": {},
        "messages": messages or [{"id": "m1", "role": "user", "content": "oi"}],
        "tools": [],
        "context": [],
        "forwardedProps": forwarded or {},
    }


def _mount(monkeypatch: pytest.MonkeyPatch, agents: list[Agent], teams: list[Team], **env: str) -> FastAPI:
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    values = {
        "APP_HOST": ALL_INTERFACES,
        "ENVIRONMENT": "production",
        "API_KEY_RUN": RUN_KEY,
        "API_KEY_ADMIN": ADMIN_KEY,
    }
    for name, value in {**values, **env}.items():
        monkeypatch.setenv(name, value)
    factory = AppFactory()
    app = factory.create_app()
    mount_agent_os(factory, app, agents, teams)
    return app


def _agent(agent_id: str, responses: list[str | ModelResponse], **kwargs: Any) -> Agent:
    return Agent(id=agent_id, name=agent_id, model=FakeChatModel(responses=list(responses)), telemetry=False, **kwargs)


def _model(entity: Agent | Team) -> FakeChatModel:
    assert isinstance(entity.model, FakeChatModel)
    return entity.model


# ── sequência de eventos válida pelo protocolo ───────────────────────


def test_texto_simples_de_agente_team_e_alias_seguem_o_protocolo(monkeypatch: pytest.MonkeyPatch):
    agent_a = _agent("agente-a", ["resposta A"])
    agent_b = _agent("agente-b", ["resposta B"])
    member = _agent("membro", ["nunca"])
    team = Team(
        id="time-1", name="time-1", members=[member], model=FakeChatModel(responses=["resposta T"]), telemetry=False
    )
    client = TestClient(_mount(monkeypatch, [agent_a, agent_b, member], [team]))

    seen: dict[str, str] = {}
    for path in ("/agui/agente-a", "/agui/agente-b", "/agui/time-1"):
        response = client.post(path, json=_body(), headers=RUN)
        events = parse_agui_sse(response.text)
        assert_valid_run(events)
        assert isinstance(events[-1], RunFinishedEvent)
        seen[path] = text_of(events)

    assert seen == {"/agui/agente-a": "resposta A", "/agui/agente-b": "resposta B", "/agui/time-1": "resposta T"}
    # o alias roda a primeira entidade (agente-a) e o roteiro dela só tinha 1 resposta: já consumida
    assert len(_model(agent_a).calls) == 1


def test_resposta_vazia_do_modelo_ainda_gera_sequencia_valida(monkeypatch: pytest.MonkeyPatch):
    client = TestClient(_mount(monkeypatch, [_agent("vazio", [""])], []))

    events = parse_agui_sse(client.post("/agui/vazio", json=_body(), headers=RUN).text)

    assert_valid_run(events)
    assert isinstance(events[-1], RunFinishedEvent)


def test_texto_seguido_de_erro_no_run_fecha_a_mensagem_antes_do_run_error(monkeypatch: pytest.MonkeyPatch):
    """Texto parcial + tool call e o roteiro esgota na 2a chamada: nada de TEXT_MESSAGE aberta."""
    agent = _agent("parcial", [_tool_call("inexistente", {}, content="começando")])
    client = TestClient(_mount(monkeypatch, [agent], []))

    response = client.post("/agui/parcial", json=_body(), headers=RUN)

    events = parse_agui_sse(response.text)
    assert_valid_run(events)
    assert isinstance(events[-1], RunErrorEvent) and events[-1].code == "run_error"
    assert "começando" in text_of(events)
    assert "RUN_FINISHED" not in types_of(events)


# ── tool call via AG-UI ──────────────────────────────────────────────


class _ScriptedModelFactory(IModelFactory):
    def __init__(self, scripts: dict[str, list[str | ModelResponse]]) -> None:
        self.models = {key: FakeChatModel(id=key, responses=list(script)) for key, script in scripts.items()}

    def create_model(self, config: ModelConfig) -> FakeChatModel:
        return self.models[config.model_id]


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Troca o transporte do ``httpx.AsyncClient`` por um ``MockTransport`` que registra os requests."""
    requests: list[httpx.Request] = []
    real_client: Callable[..., httpx.AsyncClient] = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"logradouro": "Praça da Sé"})

    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    monkeypatch.setattr(agent_factory_service, "MongoAgentDb", lambda **_: None)
    return requests


async def _http_agent(models: _ScriptedModelFactory, agent_id: str) -> Agent:
    service = AgentFactoryService(
        db_url="mongodb://mongo.invalid:27017",
        logger=RecordingLogger(),
        model_factory=models,
        embedder_factory=FakeEmbedderFactory(),
        tool_factory=HttpToolFactory(logger=RecordingLogger()),
        tool_repository=InMemoryToolRepository([CEP]),
    )
    config = AgentConfig(
        id=agent_id,
        nome=agent_id,
        factory_ia_model="fake",
        model=agent_id,
        descricao="d",
        prompt="p",
        tools_ids=["cep"],
    )
    return await service.create_agent(config)


async def test_tool_http_via_agui_emite_tool_call_e_devolve_resultado_ao_modelo(
    monkeypatch: pytest.MonkeyPatch, upstream: list[httpx.Request]
):
    models = _ScriptedModelFactory({"a1": [_tool_call("cep", {"cep": "01001000"}, "tc-1"), "O endereço é Praça da Sé"]})
    agent = await _http_agent(models, "a1")
    client = TestClient(_mount(monkeypatch, [agent], []))

    response = client.post("/agui/a1", json=_body(), headers=RUN)

    events = parse_agui_sse(response.text)
    assert_valid_run(events)
    assert isinstance(events[-1], RunFinishedEvent)
    assert [(r.method, str(r.url)) for r in upstream] == [("GET", f"{BASE}/cep/01001000")]
    starts = [e for e in events if isinstance(e, ToolCallStartEvent)]
    args = [e for e in events if isinstance(e, ToolCallArgsEvent)]
    results = [e for e in events if isinstance(e, ToolCallResultEvent)]
    assert [(s.tool_call_id, s.tool_call_name) for s in starts] == [("tc-1", "cep")]
    assert json.loads(args[0].delta) == {"cep": "01001000"}
    assert "Praça da Sé" in results[0].content and results[0].tool_call_id == "tc-1"
    # TOOL_CALL_START tem como pai uma mensagem de texto emitida antes (START/END balanceados)
    parents = {e.message_id for e in events if isinstance(e, TextMessageStartEvent)}
    assert starts[0].parent_message_id in parents
    order = types_of(events)
    assert order.index("TOOL_CALL_START") < order.index("TOOL_CALL_ARGS") < order.index("TOOL_CALL_END")
    assert order.index("TOOL_CALL_END") < order.index("TOOL_CALL_RESULT")
    assert text_of(events) == "O endereço é Praça da Sé"
    # o resultado da tool chegou ao modelo na 2a chamada
    assert any(role == "tool" and "Praça da Sé" in content for role, content in models.models["a1"].calls[1].messages)


async def test_tool_http_com_falha_do_upstream_nao_quebra_o_stream_nem_vaza_o_erro(
    monkeypatch: pytest.MonkeyPatch, upstream: list[httpx.Request]
):
    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("conexão recusada em 10.0.0.9 token=segredo-xyz", request=request)

    real_client: Callable[..., httpx.AsyncClient] = httpx.AsyncClient
    monkeypatch.setattr(httpx, "AsyncClient", lambda **kw: real_client(transport=httpx.MockTransport(boom), **kw))
    models = _ScriptedModelFactory({"a1": [_tool_call("cep", {"cep": "01001000"}), "não consegui"]})
    agent = await _http_agent(models, "a1")
    client = TestClient(_mount(monkeypatch, [agent], []))

    response = client.post("/agui/a1", json=_body(), headers=RUN)

    events = parse_agui_sse(response.text)
    assert_valid_run(events)
    assert isinstance(events[-1], RunFinishedEvent) and text_of(events) == "não consegui"


# ── team route / coordinate via AG-UI ────────────────────────────────


@pytest.mark.parametrize("mode", [TeamMode.coordinate, TeamMode.route])
def test_team_delega_ao_membro_via_agui_e_o_stream_segue_valido(monkeypatch: pytest.MonkeyPatch, mode: TeamMode):
    member = _agent("membro", ["resposta do membro"])
    script: list[str | ModelResponse] = [
        _tool_call("delegate_task_to_member", {"member_id": "membro", "task": "diga algo"}),
    ]
    if mode == TeamMode.coordinate:
        script.append("síntese do time")
    team = Team(
        id="time",
        name="time",
        mode=mode,
        members=[member],
        respond_directly=(mode == TeamMode.route),
        model=FakeChatModel(responses=script),
        telemetry=False,
    )
    client = TestClient(_mount(monkeypatch, [member], [team]))

    response = client.post("/agui/time", json=_body(), headers=RUN)

    events = parse_agui_sse(response.text)
    assert_valid_run(events)
    assert isinstance(events[-1], RunFinishedEvent), response.text
    assert len(_model(member).calls) == 1  # o membro foi de fato acionado
    assert "resposta do membro" in text_of(events) + "".join(
        e.content for e in events if isinstance(e, ToolCallResultEvent)
    )
    if mode == TeamMode.coordinate:
        assert "síntese do time" in text_of(events)


def test_team_com_membro_que_falha_conclui_sem_run_error_se_o_team_responde(monkeypatch: pytest.MonkeyPatch):
    """Erro de um membro tem outro run_id: não pode encerrar o run do team."""
    member = _agent("membro", [])  # roteiro vazio: o run do membro falha
    team = Team(
        id="time",
        name="time",
        mode=TeamMode.coordinate,
        members=[member],
        model=FakeChatModel(
            responses=[_tool_call("delegate_task_to_member", {"member_id": "membro", "task": "x"}), "o time seguiu"]
        ),
        telemetry=False,
    )
    client = TestClient(_mount(monkeypatch, [], [team]))

    events = parse_agui_sse(client.post("/agui/time", json=_body(), headers=RUN).text)

    assert_valid_run(events)
    assert isinstance(events[-1], RunFinishedEvent)
    assert "o time seguiu" in text_of(events)


# ── multi-turn / sessão ──────────────────────────────────────────────


def test_multi_turn_com_historico_chega_ao_modelo_e_a_sessao_e_a_do_thread_id(monkeypatch: pytest.MonkeyPatch):
    agent = _agent("conversa", ["primeira", "segunda", "terceira"])
    client = TestClient(_mount(monkeypatch, [agent], []))
    thread = "thread-fixa"

    first = client.post("/agui/conversa", json=_body(thread_id=thread, run_id="r-1"), headers=RUN)
    history = [
        {"id": "m1", "role": "user", "content": "oi"},
        {"id": "m2", "role": "assistant", "content": "primeira"},
        {"id": "m3", "role": "user", "content": "e agora?"},
    ]
    second = client.post("/agui/conversa", json=_body(history, thread, "r-2"), headers=RUN)
    third = client.post("/agui/conversa", json=_body(thread_id="outro-thread", run_id="r-3"), headers=RUN)

    for response in (first, second, third):
        events = parse_agui_sse(response.text)
        assert_valid_run(events)
        assert isinstance(events[-1], RunFinishedEvent)
    assert [e.thread_id for e in parse_agui_sse(second.text) if isinstance(e, RunFinishedEvent)] == [thread]
    calls = _model(agent).calls
    assert len(calls) == 3
    roles_second = [role for role, _ in calls[1].messages if role != "system"]
    assert roles_second == ["user", "assistant", "user"]
    assert calls[1].last_user_message == "e agora?"
    assert [c for r, c in calls[1].messages if r == "assistant"] == ["primeira"]
    # outro thread_id => outra conversa: sem o assistant "primeira" do histórico do cliente
    assert "assistant" not in [role for role, _ in calls[2].messages]


# ── desconexão do cliente no meio do stream ──────────────────────────


class _SlowModel(FakeChatModel):
    """Modelo que entrega o 1o trecho e depois fica pendurado até ser cancelado."""

    started: asyncio.Event | None = None
    cancelled = False

    async def ainvoke_stream(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def,override]
        yield ModelResponse(role="assistant", content="trecho 1")
        self.started = self.started or asyncio.Event()
        self.started.set()
        try:
            await asyncio.sleep(3600)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        yield ModelResponse(role="assistant", content="nunca")


@contextmanager
def _serve(app: FastAPI, host: str = "127.0.0.1") -> Iterator[int]:
    """uvicorn real num socket já ligado a uma porta efêmera (sem a corrida de achar porta livre)."""
    listener = socket.socket()
    listener.bind((host, 0))
    port = int(listener.getsockname()[1])
    config = uvicorn.Config(app, host=host, port=port, log_level="warning", lifespan="off", ws="websockets-sansio")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=lambda: asyncio.run(server.serve(sockets=[listener])), daemon=True)
    try:
        thread.start()
        deadline = time.monotonic() + 15
        while not server.started:
            if time.monotonic() > deadline or not thread.is_alive():
                raise RuntimeError("uvicorn de teste não subiu")
            time.sleep(0.02)
        yield port
    finally:
        server.should_exit = True
        thread.join(timeout=15)
        listener.close()


def test_uvicorn_real_sse_completo_de_agente_team_e_alias_com_chave(monkeypatch: pytest.MonkeyPatch):
    member = _agent("membro", ["m"])
    agent = _agent("agente-a", ["texto A", "texto A2"])
    team = Team(
        id="time-1", name="time-1", members=[member], model=FakeChatModel(responses=["texto T"]), telemetry=False
    )
    app = _mount(
        monkeypatch,
        [agent, member],
        [team],
        APP_HOST="127.0.0.1",
        CORS_ALLOWED_ORIGINS="https://painel.example.com",
    )

    with _serve(app) as port, httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=20) as client:
        assert client.post("/agui/agente-a", json=_body()).status_code == 401
        assert (
            client.post("/agui/agente-a", json=_body(), headers={"Authorization": "Bearer errada"}).status_code == 401
        )

        with client.stream("POST", "/agui/agente-a", json=_body(), headers=RUN) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert "access-control-allow-origin" not in response.headers
            chunks = list(response.iter_text())
        events = parse_agui_sse("".join(chunks))
        assert_valid_run(events)
        assert text_of(events) == "texto A"

        team_events = parse_agui_sse(client.post("/agui/time-1", json=_body(), headers=RUN).text)
        assert_valid_run(team_events)
        assert text_of(team_events) == "texto T"

        alias = client.post("/agui", json=_body(), headers={"Origin": "https://painel.example.com", **RUN})
        assert alias.headers["deprecation"] == "true"
        assert alias.headers["access-control-allow-origin"] == "https://painel.example.com"
        assert text_of(parse_agui_sse(alias.text)) == "texto A2"

        evil = client.post("/agui/time-1", json=_body(), headers={"Origin": "https://evil.example.com", **RUN})
        assert "access-control-allow-origin" not in evil.headers


def test_uvicorn_real_desconexao_no_meio_do_stream_cancela_o_run_e_o_servidor_segue_de_pe(
    monkeypatch: pytest.MonkeyPatch,
):
    slow = _SlowModel(responses=[])
    hanging = Agent(id="pendurado", name="pendurado", model=slow, telemetry=False)
    ok = _agent("ok", ["tudo certo"] * 3)
    app = _mount(monkeypatch, [hanging, ok], [], APP_HOST="127.0.0.1")

    with _serve(app) as port:
        base = f"http://127.0.0.1:{port}"
        # cliente cru: lê o 1o trecho e fecha o socket sem terminar o stream
        body = json.dumps(_body()).encode()
        request = (
            f"POST /agui/pendurado HTTP/1.1\r\nHost: 127.0.0.1:{port}\r\nAuthorization: Bearer {RUN_KEY}\r\n"
            f"Content-Type: application/json\r\nContent-Length: {len(body)}\r\nConnection: close\r\n\r\n"
        ).encode() + body
        received = b""
        with socket.create_connection(("127.0.0.1", port), timeout=15) as sock:
            sock.sendall(request)
            deadline = time.monotonic() + 15
            while b"trecho 1" not in received and time.monotonic() < deadline:
                received += sock.recv(4096)
        assert b"RUN_STARTED" in received and b"trecho 1" in received

        deadline = time.monotonic() + 10
        while not slow.cancelled and time.monotonic() < deadline:
            time.sleep(0.05)
        assert slow.cancelled, "o run do cliente desconectado continuou pendurado no servidor"

        with httpx.Client(base_url=base, timeout=20) as client:
            events = parse_agui_sse(client.post("/agui/ok", json=_body(), headers=RUN).text)
        assert_valid_run(events)
        assert text_of(events) == "tudo certo"


# ── estado que não pode vazar ────────────────────────────────────────


def test_sessao_do_agno_com_db_mantem_o_historico_pelo_thread_id_sem_o_cliente_reenviar(
    monkeypatch: pytest.MonkeyPatch,
):
    from agno.db.in_memory import InMemoryDb

    agent = _agent("sessao", ["um", "dois", "tres"], db=InMemoryDb(), add_history_to_context=True)
    client = TestClient(_mount(monkeypatch, [agent], []))

    for index, thread in enumerate(["t-A", "t-A", "t-B"], start=1):
        content = f"pergunta {index}"
        response = client.post(
            "/agui/sessao",
            json=_body([{"id": f"m{index}", "role": "user", "content": content}], thread, f"run-{index}"),
            headers=RUN,
        )
        assert_valid_run(parse_agui_sse(response.text))

    calls = _model(agent).calls
    contents_2 = [c for r, c in calls[1].messages if r in ("user", "assistant")]
    assert "pergunta 1" in contents_2 and "um" in contents_2 and "pergunta 2" in contents_2
    contents_3 = [c for r, c in calls[2].messages if r in ("user", "assistant")]
    assert "pergunta 1" not in contents_3 and "um" not in contents_3  # outra thread: outra sessão


def _active_runs() -> dict[str, bool]:
    from agno.run import cancel

    return cancel.get_cancellation_manager().get_active_runs()


def test_run_concluido_ou_com_erro_nao_deixa_estado_no_gerenciador_de_cancelamento(monkeypatch: pytest.MonkeyPatch):
    client = TestClient(_mount(monkeypatch, [_agent("ok", ["a"]), _agent("quebrado", [])], []))

    ok = client.post("/agui/ok", json=_body(run_id="run-ok"), headers=RUN)
    broken = client.post("/agui/quebrado", json=_body(run_id="run-quebrado"), headers=RUN)

    assert isinstance(parse_agui_sse(ok.text)[-1], RunFinishedEvent)
    assert isinstance(parse_agui_sse(broken.text)[-1], RunErrorEvent)
    assert _active_runs() == {}


def test_runs_agui_nao_mexem_nas_metricas_de_run_nem_no_gauge(monkeypatch: pytest.MonkeyPatch):
    """Decisão registrada (F1-08): AG-UI fica fora do MetricsMiddleware até a F8; nada pode vazar."""
    from src.infrastructure.web import metrics_middleware
    from tests.fakes import RecordingTelemetryMetrics

    recording = RecordingTelemetryMetrics()
    monkeypatch.setattr(metrics_middleware, "TelemetryMetrics", lambda: recording)
    client = TestClient(_mount(monkeypatch, [_agent("ok", ["a"]), _agent("quebrado", [])], []))

    client.post("/agui/ok", json=_body(), headers=RUN)
    client.post("/agui/quebrado", json=_body(), headers=RUN)
    client.post("/agui/nao-existe", json=_body(), headers=RUN)

    assert not recording.agent_requests and not recording.agent_errors
    assert not any(recording.active.values())


def test_desconexao_no_meio_do_stream_libera_o_gerenciador_de_cancelamento(monkeypatch: pytest.MonkeyPatch):
    """Cliente que some (ASGI direto, disconnect no meio do corpo): o run é cancelado e limpo."""
    slow = _SlowModel(responses=[])
    app = _mount(monkeypatch, [Agent(id="lento", name="lento", model=slow, telemetry=False)], [], APP_HOST="127.0.0.1")
    body = json.dumps(_body(run_id="run-cortado")).encode()

    async def drive() -> list[dict[str, Any]]:
        sent: list[dict[str, Any]] = []
        disconnect = asyncio.Event()
        delivered = False

        async def receive() -> dict[str, Any]:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": body, "more_body": False}
            await disconnect.wait()
            return {"type": "http.disconnect"}

        async def send(message: dict[str, Any]) -> None:
            sent.append(message)
            if message["type"] == "http.response.body" and b"trecho 1" in message.get("body", b""):
                disconnect.set()

        scope = {
            "type": "http",
            "asgi": {"version": "3.0"},
            "http_version": "1.1",
            "method": "POST",
            "path": "/agui/lento",
            "raw_path": b"/agui/lento",
            "query_string": b"",
            "root_path": "",
            "scheme": "http",
            "headers": [
                (b"host", b"127.0.0.1:8000"),
                (b"authorization", f"Bearer {RUN_KEY}".encode()),
                (b"content-type", b"application/json"),
                (b"content-length", str(len(body)).encode()),
            ],
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 8000),
            "state": {},
        }
        await asyncio.wait_for(app(scope, receive, send), timeout=15)
        return sent

    sent = asyncio.run(drive())

    assert any(b"trecho 1" in m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert not any(b"RUN_FINISHED" in m.get("body", b"") for m in sent if m["type"] == "http.response.body")
    assert slow.cancelled
    assert _active_runs() == {}


# ── cancelamento e concorrência (uvicorn real) ───────────────────────


class _TickingModel(FakeChatModel):
    """Modelo que emite um trecho a cada 20 ms (até 500), dando ao agno onde checar o cancelamento."""

    async def ainvoke_stream(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def,override]
        for index in range(500):
            yield ModelResponse(role="assistant", content=f"t{index} ")
            await asyncio.sleep(0.02)


def test_uvicorn_real_cancel_do_agno_encerra_o_run_agui_com_run_error_cancelado(monkeypatch: pytest.MonkeyPatch):
    ticking = Agent(id="tic", name="tic", model=_TickingModel(responses=[]), telemetry=False)
    app = _mount(monkeypatch, [ticking], [], APP_HOST="127.0.0.1")

    with _serve(app) as port, httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=30) as client:
        chunks: list[str] = []
        with client.stream("POST", "/agui/tic", json=_body(run_id="run-cancelavel"), headers=RUN) as response:
            cancelled = False
            for chunk in response.iter_text():
                chunks.append(chunk)
                if not cancelled and "t1 " in "".join(chunks):
                    cancel = httpx.post(
                        f"http://127.0.0.1:{port}/agents/tic/runs/run-cancelavel/cancel", headers=RUN, timeout=20
                    )
                    assert cancel.status_code == 200, cancel.text
                    cancelled = True
            assert cancelled

        events = parse_agui_sse("".join(chunks))
    assert_valid_run(events)
    assert isinstance(events[-1], RunErrorEvent)
    assert events[-1].code == "run_cancelled" and events[-1].message == "Run cancelado."
    assert "RUN_FINISHED" not in types_of(events)


async def test_runs_concorrentes_em_entidades_diferentes_nao_misturam_respostas(monkeypatch: pytest.MonkeyPatch):
    agents = [_agent(f"ag-{i}", [f"resposta {i}"] * 10) for i in range(3)]
    member = _agent("membro", ["m"])
    team = Team(
        id="tm", name="tm", members=[member], model=FakeChatModel(responses=["resposta tm"] * 10), telemetry=False
    )
    app = _mount(monkeypatch, [*agents, member], [team])
    expected = {f"ag-{i}": f"resposta {i}" for i in range(3)} | {"tm": "resposta tm"}

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
        jobs = [(entity, n) for n in range(5) for entity in expected]
        responses = await asyncio.gather(
            *(client.post(f"/agui/{entity}", json=_body(run_id=f"run-{entity}-{n}"), headers=RUN) for entity, n in jobs)
        )

    for (entity, n), response in zip(jobs, responses, strict=True):
        events = parse_agui_sse(response.text)
        assert_valid_run(events)
        assert text_of(events) == expected[entity]
        assert events[0].run_id == f"run-{entity}-{n}"  # type: ignore[attr-defined]
    assert _active_runs() == {}


# ── ciclo de vida real (lifespan) ────────────────────────────────────


def test_lifespan_real_monta_o_agui_por_entidade_e_desmonta_sem_resto(monkeypatch: pytest.MonkeyPatch):
    """Startup pelo lifespan (não só ``mount_agent_os``): carga de entidades -> rotas -> shutdown."""
    from types import SimpleNamespace

    from src.infrastructure.web import app_factory

    agent = _agent("agente-a", ["pelo lifespan"])
    member = _agent("membro", ["m"])
    team = Team(id="time-1", name="time-1", members=[member], model=FakeChatModel(responses=["time"]), telemetry=False)
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    factory = AppFactory()
    app = factory.create_app()
    cleaned: list[bool] = []

    runtime = agno_runtime()

    async def ensure_container() -> Any:
        async def cleanup() -> None:
            cleaned.append(True)

        factory._container = SimpleNamespace(  # type: ignore[assignment]
            config=factory._config, cleanup=cleanup, get_agent_runtime=lambda: runtime
        )
        return factory._container

    async def load_all(_container: object) -> tuple[list[Agent], list[Team]]:
        return [agent, member], [team]

    monkeypatch.setattr(factory, "_ensure_container", ensure_container)
    monkeypatch.setattr(factory, "_load_all_entities", load_all)
    monkeypatch.setattr(app_factory, "setup_telemetry", lambda config: None)
    monkeypatch.setattr(app_factory, "shutdown_telemetry", lambda: None)
    local = {"base_url": "http://127.0.0.1:7777", "client": ("127.0.0.1", 50000)}

    with TestClient(app, **local) as client:  # type: ignore[arg-type]
        alias = client.post("/agui", json=_body())
        by_id = client.post("/agui/time-1", json=_body())
        missing = client.post("/agui/nao-existe", json=_body())

    assert alias.headers["deprecation"] == "true" and text_of(parse_agui_sse(alias.text)) == "pelo lifespan"
    assert text_of(parse_agui_sse(by_id.text)) == "time"
    assert missing.status_code == 404
    assert cleaned == [True]
