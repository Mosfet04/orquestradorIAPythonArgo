"""F1-08 (B4): AG-UI por entidade, de ponta a ponta.

Pilha real: ``AppFactory.create_app`` (CORS + auth por chave) -> ``_mount_agent_os`` com
2 agentes e 1 team de ``FakeChatModel`` roteirizado -> ``POST /agui/{id}`` e o alias
``POST /agui``. Antes: o agno 2.5.8 criava uma rota ``POST /agui`` por interface e
descartava as repetidas (só a primeira entidade respondia), fixava
``Access-Control-Allow-Origin: *`` na resposta SSE e descartava o erro do run (o cliente
via RUN_STARTED + RUN_FINISHED sem motivo).
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Callable
from typing import Any

import pytest
from agno.agent import Agent
from agno.team import Team
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.infrastructure.runtime.agno.user_id_guardrail import UserIdRequiredGuardrail
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import FakeChatModel

RUN_KEY = "agui-run-key-" + "r" * 21
ADMIN_KEY = "agui-admin-key-" + "a" * 19
ALLOWED = "https://painel.example.com"
DENIED = "https://evil.example.com"
ALL_INTERFACES = "0.0.0" + ".0"  # bind recusado sem chaves: o app sobe em modo produção
PROD = {"APP_HOST": ALL_INTERFACES, "ENVIRONMENT": "production", "API_KEY_RUN": RUN_KEY, "API_KEY_ADMIN": ADMIN_KEY}
RUN = {"Authorization": f"Bearer {RUN_KEY}"}
_ENV_NAMES = ("ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS", "APP_HOST", "API_KEY_RUN", "API_KEY_ADMIN")

ANSWERS = {"agente-a": "sou o agente A", "agente-b": "sou o agente B", "time-1": "sou o time"}


def _body(run_id: str = "r1", forwarded: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "threadId": f"t-{uuid.uuid4().hex[:8]}",
        "runId": run_id,
        "state": {},
        "messages": [{"id": "m1", "role": "user", "content": "quem é você?"}],
        "tools": [],
        "context": [],
        "forwardedProps": forwarded or {},
    }


def _sse(text: str) -> list[dict[str, Any]]:
    return [json.loads(line.removeprefix("data: ")) for line in text.splitlines() if line.startswith("data: ")]


def _types(text: str) -> list[str]:
    return [event["type"] for event in _sse(text)]


def _text(text: str) -> str:
    return "".join(e["delta"] for e in _sse(text) if e["type"] == "TEXT_MESSAGE_CONTENT")


class Mounted:
    """App montado + as entidades (para olhar o modelo de cada uma)."""

    def __init__(self, app: FastAPI, agents: list[Agent], teams: list[Team]) -> None:
        self.app = app
        self.client = TestClient(app)
        self.entities: dict[str, Agent | Team] = {str(e.id): e for e in [*agents, *teams]}

    def model(self, entity_id: str) -> FakeChatModel:
        model = self.entities[entity_id].model
        assert isinstance(model, FakeChatModel)
        return model


@pytest.fixture
def mount(monkeypatch: pytest.MonkeyPatch) -> Callable[..., Mounted]:
    def _mount(agents: list[Agent] | None = None, teams: list[Team] | None = None) -> Mounted:
        for name in _ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("AGNO_TELEMETRY", "false")
        monkeypatch.setenv("CORS_ALLOWED_ORIGINS", ALLOWED)
        for name, value in PROD.items():
            monkeypatch.setenv(name, value)
        if agents is None and teams is None:
            agents = [_agent("agente-a"), _agent("agente-b")]
            teams = [_team("time-1")]
        factory = AppFactory()
        app = factory.create_app()
        factory._mount_agent_os(app, agents or [], teams or [])
        return Mounted(app, agents or [], teams or [])

    return _mount


def _agent(agent_id: str, responses: list[str] | None = None, **kwargs: Any) -> Agent:
    answers = [ANSWERS.get(agent_id, "ok")] * 5 if responses is None else responses
    return Agent(id=agent_id, name=agent_id, model=FakeChatModel(responses=answers), telemetry=False, **kwargs)


def _team(team_id: str, responses: list[str] | None = None) -> Team:
    answers = [ANSWERS.get(team_id, "ok")] * 5 if responses is None else responses
    return Team(id=team_id, name=team_id, members=[], model=FakeChatModel(responses=answers), telemetry=False)


# ── uma rota por entidade ───────────────────────────────────────────


@pytest.mark.parametrize("entity_id", list(ANSWERS))
def test_cada_entidade_responde_pelo_proprio_id_sem_misturar(mount: Callable[..., Mounted], entity_id: str):
    mounted = mount()

    response = mounted.client.post(f"/agui/{entity_id}", json=_body(), headers=RUN)

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert _types(response.text)[0] == "RUN_STARTED" and _types(response.text)[-1] == "RUN_FINISHED"
    assert _text(response.text) == ANSWERS[entity_id]
    for other in ANSWERS:
        assert len(mounted.model(other).calls) == (1 if other == entity_id else 0)
    assert "deprecation" not in response.headers


def test_alias_agui_responde_pela_primeira_entidade_e_sinaliza_deprecated(mount: Callable[..., Mounted]):
    mounted = mount()

    response = mounted.client.post("/agui", json=_body(), headers=RUN)

    assert response.status_code == 200
    assert _text(response.text) == ANSWERS["agente-a"]
    assert response.headers["deprecation"] == "true"
    assert response.headers["link"] == '</agui/agente-a>; rel="successor-version"'
    assert mounted.model("agente-b").calls == [] and mounted.model("time-1").calls == []


def test_navegador_de_origem_permitida_le_o_sinal_de_deprecated(mount: Callable[..., Mounted]):
    mounted = mount()

    allowed = mounted.client.post("/agui", json=_body(), headers={"Origin": ALLOWED, **RUN})
    denied = mounted.client.post("/agui", json=_body(), headers={"Origin": DENIED, **RUN})

    assert allowed.headers["access-control-expose-headers"] == "Deprecation, Link"
    assert allowed.headers["deprecation"] == "true"
    # O Starlette põe Expose-Headers em toda resposta com Origin; sem ACAO o navegador o ignora.
    assert "access-control-allow-origin" not in denied.headers


def test_alias_sem_agentes_aponta_para_o_primeiro_team(mount: Callable[..., Mounted]):
    mounted = mount([], [_team("time-1")])

    response = mounted.client.post("/agui", json=_body(), headers=RUN)

    assert _text(response.text) == ANSWERS["time-1"]
    assert response.headers["link"] == '</agui/time-1>; rel="successor-version"'


@pytest.mark.parametrize("path", ["/agui/nao-existe", "/agui/membro-x", "/agui/AGENTE-A", "/agui/agente-a/extra"])
def test_id_inexistente_responde_404_sem_rodar_nada(mount: Callable[..., Mounted], path: str):
    mounted = mount()

    response = mounted.client.post(path, json=_body(), headers=RUN)

    assert response.status_code == 404
    assert all(mounted.model(entity_id).calls == [] for entity_id in ANSWERS)


@pytest.mark.parametrize("path", ["/agui/agente-a", "/agui/time-1", "/agui", "/agui/nao-existe"])
def test_sem_chave_responde_401(mount: Callable[..., Mounted], path: str):
    mounted = mount()

    response = mounted.client.post(path, json=_body())

    assert response.status_code == 401 and response.json() == {"detail": "unauthorized"}
    assert all(mounted.model(entity_id).calls == [] for entity_id in ANSWERS)


# ── CORS: quem decide é o CORSMiddleware do app ─────────────────────


@pytest.mark.parametrize("path", ["/agui/agente-b", "/agui/time-1", "/agui"])
def test_origem_negada_nao_recebe_acao_e_permitida_recebe_so_a_propria(mount: Callable[..., Mounted], path: str):
    mounted = mount()

    denied = mounted.client.post(path, json=_body(), headers={"Origin": DENIED, **RUN})
    allowed = mounted.client.post(path, json=_body(), headers={"Origin": ALLOWED, **RUN})

    assert denied.status_code == 200
    assert "access-control-allow-origin" not in denied.headers
    assert "access-control-allow-headers" not in denied.headers
    assert "access-control-allow-methods" not in denied.headers
    assert allowed.headers.get_list("access-control-allow-origin") == [ALLOWED]


# ── erro do run vira RUN_ERROR ──────────────────────────────────────


@pytest.mark.parametrize("kind", ["agent", "team"])
def test_run_com_erro_do_modelo_emite_run_error_generico(mount: Callable[..., Mounted], kind: str):
    """Roteiro esgotado: o agno captura a exceção e emite RunError; o texto dela não vaza."""
    if kind == "agent":
        mounted = mount([_agent("quebrado", responses=[])], [])
    else:
        mounted = mount([], [_team("quebrado", responses=[])])

    response = mounted.client.post("/agui/quebrado", json=_body("run-erro"), headers=RUN)

    assert response.status_code == 200
    events = _sse(response.text)
    assert [e["type"] for e in events] == ["RUN_STARTED", "RUN_ERROR"]
    error = events[-1]
    assert error["code"] == "run_error"
    assert error["message"] == "Falha ao executar o run; detalhes no log do servidor."
    assert "roteiro" not in response.text and "FakeChatModel" not in response.text
    assert mounted.model("quebrado").exhausted


def test_run_recusado_por_falta_de_user_id_emite_run_error_com_o_motivo(mount: Callable[..., Mounted]):
    agent = _agent("com-memoria", pre_hooks=[UserIdRequiredGuardrail("com-memoria")])
    mounted = mount([agent], [])

    refused = mounted.client.post("/agui/com-memoria", json=_body("r-anon"), headers=RUN)
    accepted = mounted.client.post("/agui/com-memoria", json=_body("r-ana", {"user_id": "ana"}), headers=RUN)

    events = _sse(refused.text)
    assert [e["type"] for e in events] == ["RUN_STARTED", "RUN_ERROR"]
    assert events[-1]["code"] == "input_check_error"
    assert events[-1]["message"].startswith("user_id obrigatório: 'com-memoria'")
    assert _types(accepted.text)[-1] == "RUN_FINISHED" and "RUN_ERROR" not in accepted.text
    assert len(mounted.model("com-memoria").calls) == 1


# ── runId do cliente ────────────────────────────────────────────────

HOSTILE_RUN_IDS = {
    "longo": "r" * 65,
    "muito-longo": "x" * 10_000,
    "controle": "r1\n\x00data: {}",
    "espaco": "r 1",
    "unicode": "r​1",
    "barra": "../r1",
    "vazio": "",
}


@pytest.mark.parametrize("run_id", HOSTILE_RUN_IDS.values(), ids=HOSTILE_RUN_IDS)
def test_run_id_hostil_e_trocado_por_um_gerado_pelo_servidor(mount: Callable[..., Mounted], run_id: str):
    mounted = mount()

    response = mounted.client.post("/agui/agente-a", json=_body(run_id), headers=RUN)

    events = _sse(response.text)
    started, finished = events[0], events[-1]
    assert started["type"] == "RUN_STARTED" and finished["type"] == "RUN_FINISHED"
    assert uuid.UUID(started["runId"]).version == 4
    assert finished["runId"] == started["runId"]
    assert _text(response.text) == ANSWERS["agente-a"]
    if run_id:
        assert run_id not in response.text


@pytest.mark.parametrize("run_id", ["r1", "run_ABC-123", "3f2b8c1e-9d4a-4e7b-8f6a-2c5d1e0b7a94", "a" * 64])
def test_run_id_valido_do_cliente_e_mantido(mount: Callable[..., Mounted], run_id: str):
    mounted = mount()

    response = mounted.client.post("/agui/time-1", json=_body(run_id), headers=RUN)

    events = _sse(response.text)
    assert events[0]["runId"] == run_id and events[-1]["runId"] == run_id
