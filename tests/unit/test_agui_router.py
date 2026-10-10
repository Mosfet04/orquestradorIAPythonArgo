"""Router AG-UI por entidade (F1-08): registro, runId e tradução de falha de run.

O E2E com a pilha real (auth, CORS, AgentOS) está em
``tests/security/test_agui_per_entity.py``. Aqui o router roda num FastAPI nu, com
``Agent``/``Team`` reais de ``FakeChatModel`` ou com uma entidade que devolve um roteiro de
eventos do agno (para os casos que o modelo fake não produz: exceção fora do stream,
cancelamento, erro de um membro).
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Sequence
from typing import Any

import pytest
from agno.agent import Agent
from agno.run.agent import RunCancelledEvent, RunContentEvent, RunErrorEvent
from agno.team import Team
from fastapi import FastAPI
from starlette.testclient import TestClient

from src.infrastructure.runtime.agno.agui_router import build_agui_router, resolve_run_id
from tests.fakes import FakeChatModel, RecordingLogger

SECRET_LIKE = "sk-" + "x" * 20  # valor de teste: simula texto sensível numa exceção


def _body(run_id: str = "r1") -> dict[str, Any]:
    return {
        "threadId": "t1",
        "runId": run_id,
        "state": {},
        "messages": [{"id": "m1", "role": "user", "content": "oi"}],
        "tools": [],
        "context": [],
        "forwardedProps": {},
    }


def _sse(text: str) -> list[dict[str, Any]]:
    return [json.loads(line.removeprefix("data: ")) for line in text.splitlines() if line.startswith("data: ")]


class ScriptedAgent(Agent):
    """``Agent`` cujo ``arun`` devolve um roteiro de eventos do agno (ou levanta)."""

    def __init__(self, agent_id: str, script: Sequence[Any] = (), raises: Exception | None = None) -> None:
        super().__init__(id=agent_id, name=agent_id, model=FakeChatModel(responses=[]), telemetry=False)
        self._script = list(script)
        self._raises = raises
        self.received: dict[str, Any] = {}

    def arun(self, input: Any, **kwargs: Any) -> AsyncIterator[Any]:  # type: ignore[override]
        self.received = {"input": input, **kwargs}
        if self._raises is not None:
            raise self._raises
        return self._events()

    async def _events(self) -> AsyncIterator[Any]:
        for event in self._script:
            yield event


def _client(
    agents: Sequence[Agent] = (), teams: Sequence[Team] = (), logger: RecordingLogger | None = None
) -> TestClient:
    app = FastAPI()
    app.include_router(build_agui_router(list(agents), list(teams), logger or RecordingLogger()))
    return TestClient(app)


# ── runId ───────────────────────────────────────────────────────────


@pytest.mark.parametrize("run_id", ["r1", "A_b-9", "a" * 64])
def test_run_id_valido_e_mantido(run_id: str):
    assert resolve_run_id(run_id) == run_id


@pytest.mark.parametrize("run_id", ["", "a" * 65, "r1\n", "r 1", "r.1", "r/1", "é", "r1\x00"])
def test_run_id_invalido_vira_um_uuid_novo(run_id: str):
    generated = resolve_run_id(run_id)

    assert generated != run_id
    assert resolve_run_id(generated) == generated
    assert generated != resolve_run_id(run_id)


def test_entidade_recebe_o_run_id_resolvido_e_o_user_id_do_forwarded_props():
    agent = ScriptedAgent("a1", script=[RunContentEvent(run_id="r-ok", content="oi")])
    body = {**_body("r-ok"), "forwardedProps": {"user_id": "ana"}}

    _client([agent]).post("/agui/a1", json=body)

    assert agent.received["run_id"] == "r-ok"
    assert agent.received["user_id"] == "ana"
    assert agent.received["session_id"] == "t1"
    assert agent.received["stream"] is True


# ── falha do run ────────────────────────────────────────────────────


def test_excecao_fora_do_stream_vira_run_error_generico_e_log_sem_o_texto():
    logger = RecordingLogger()
    agent = ScriptedAgent("a1", raises=RuntimeError(f"falhou com {SECRET_LIKE}"))

    response = _client([agent], logger=logger).post("/agui/a1", json=_body())

    events = _sse(response.text)
    assert [e["type"] for e in events] == ["RUN_STARTED", "RUN_ERROR"]
    assert events[-1]["code"] == "run_error"
    assert SECRET_LIKE not in response.text
    errors = [r for r in logger.records if r.level == "error"]
    assert len(errors) == 1
    assert errors[0].context == {"entity_id": "a1", "run_id": "r1", "error_type": "RuntimeError"}
    assert all(SECRET_LIKE not in str(r.context) and SECRET_LIKE not in r.message for r in logger.records)


def test_run_error_do_agno_com_texto_de_excecao_nao_vaza():
    script = [
        RunContentEvent(run_id="r1", content="parcial"),
        RunErrorEvent(run_id="r1", content=f"Error code 401 {SECRET_LIKE}"),
    ]
    logger = RecordingLogger()

    response = _client([ScriptedAgent("a1", script=script)], logger=logger).post("/agui/a1", json=_body())

    events = _sse(response.text)
    assert [e["type"] for e in events] == [
        "RUN_STARTED",
        "TEXT_MESSAGE_START",
        "TEXT_MESSAGE_CONTENT",
        "TEXT_MESSAGE_END",
        "RUN_ERROR",
    ]
    assert events[-1]["code"] == "run_error"
    assert SECRET_LIKE not in response.text
    assert [r.context for r in logger.records if r.level == "warning"] == [
        {"entity_id": "a1", "run_id": "r1", "code": "run_error"}
    ]


def test_mensagem_de_guardrail_e_repassada_truncada():
    long_message = "entrada recusada " + "x" * 1000
    script = [RunErrorEvent(run_id="r1", content=long_message, error_type="output_check_error")]

    response = _client([ScriptedAgent("a1", script=script)]).post("/agui/a1", json=_body())

    error = _sse(response.text)[-1]
    assert error["code"] == "output_check_error"
    assert error["message"] == long_message[:300]


def test_run_cancelado_vira_run_error_cancelled():
    script = [RunCancelledEvent(run_id="r1", reason="cancelado por alguém")]

    response = _client([ScriptedAgent("a1", script=script)]).post("/agui/a1", json=_body())

    events = _sse(response.text)
    assert [e["type"] for e in events] == ["RUN_STARTED", "RUN_ERROR"]
    assert events[-1] == {"type": "RUN_ERROR", "message": "Run cancelado.", "code": "run_cancelled"}


def test_erro_de_outro_run_no_stream_nao_encerra_o_run_com_erro():
    """Evento de erro de um membro (outro run_id) não é o desfecho do run pedido."""
    script = [
        RunErrorEvent(run_id="run-do-membro", content="membro falhou"),
        RunContentEvent(run_id="r1", content="o time se recuperou"),
    ]

    response = _client([ScriptedAgent("a1", script=script)]).post("/agui/a1", json=_body())

    events = _sse(response.text)
    assert events[-1]["type"] == "RUN_FINISHED"
    assert "RUN_ERROR" not in [e["type"] for e in events]


# ── registro ────────────────────────────────────────────────────────


def test_agente_e_team_com_o_mesmo_id_o_agente_vence_com_aviso():
    logger = RecordingLogger()
    agent = Agent(id="mesmo", name="a", model=FakeChatModel(responses=["do agente"]), telemetry=False)
    team = Team(id="mesmo", name="t", members=[], model=FakeChatModel(responses=["do time"]), telemetry=False)

    response = _client([agent], [team], logger=logger).post("/agui/mesmo", json=_body())

    assert "do agente" in response.text and "do time" not in response.text
    assert [r.context for r in logger.records if r.level == "warning"] == [{"entity_id": "mesmo"}]


def test_sem_entidades_nao_ha_alias():
    client = _client()

    assert client.post("/agui", json=_body()).status_code == 404
    assert client.post("/agui/x", json=_body()).status_code == 404


def test_alias_aponta_para_a_primeira_entidade_com_link_codificado():
    agent = Agent(id="agente €/1", name="a", model=FakeChatModel(responses=["oi"]), telemetry=False)

    response = _client([agent]).post("/agui", json=_body())

    assert response.status_code == 200
    assert response.headers["deprecation"] == "true"
    assert response.headers["link"] == '</agui/agente%20%E2%82%AC%2F1>; rel="successor-version"'


def test_status_da_interface_continua_disponivel():
    assert _client().get("/status").json() == {"status": "available"}


def test_entidade_sem_id_fica_fora_do_registro_com_aviso():
    logger = RecordingLogger()
    sem_id = Agent(name="sem-id", model=FakeChatModel(responses=["nao"]), telemetry=False)
    com_id = Agent(id="a1", name="a1", model=FakeChatModel(responses=["sim"]), telemetry=False)

    response = _client([sem_id, com_id], logger=logger).post("/agui", json=_body())

    assert "sim" in response.text and response.headers["link"] == '</agui/a1>; rel="successor-version"'
    assert [r.context for r in logger.records if r.level == "warning"] == [{"entity_name": "sem-id"}]


NON_STRING_USER_IDS = {"operador-nosql": {"$ne": None}, "lista": ["ana"], "inteiro": 123, "booleano": True}


@pytest.mark.parametrize("raw", NON_STRING_USER_IDS.values(), ids=NON_STRING_USER_IDS)
def test_user_id_nao_string_chega_como_none_e_o_log_so_tem_o_tipo(raw: object):
    logger = RecordingLogger()
    agent = ScriptedAgent("a1", script=[RunContentEvent(run_id="r1", content="oi")])
    body = {**_body(), "forwardedProps": {"user_id": raw}}

    _client([agent], logger=logger).post("/agui/a1", json=body)

    assert agent.received["user_id"] is None
    infos = [r.context for r in logger.records if r.level == "info" and "user_id" in r.message]
    assert infos == [{"entity_id": "a1", "run_id": "r1", "user_id_type": type(raw).__name__}]


@pytest.mark.parametrize("forwarded", [{}, {"user_id": None}, {"outro": "x"}], ids=["vazio", "null", "sem-chave"])
def test_user_id_ausente_chega_como_none_sem_log(forwarded: dict[str, object]):
    logger = RecordingLogger()
    agent = ScriptedAgent("a1", script=[RunContentEvent(run_id="r1", content="oi")])

    _client([agent], logger=logger).post("/agui/a1", json={**_body(), "forwardedProps": forwarded})

    assert agent.received["user_id"] is None
    assert not any("user_id" in r.message for r in logger.records)


def test_log_do_run_id_trocado_nao_contem_o_valor_recebido():
    logger = RecordingLogger()
    hostile = "segredo-no-run-id\n" + "x" * 80
    agent = ScriptedAgent("a1", script=[RunContentEvent(content="oi")])

    _client([agent], logger=logger).post("/agui/a1", json=_body(hostile))

    replaced = [r for r in logger.records if "runId" in r.message]
    assert len(replaced) == 1
    assert replaced[0].message == "runId do cliente vazio ou fora do formato; gerado pelo servidor"
    assert replaced[0].context["client_run_id_length"] == len(hostile)
    assert agent.received["run_id"] == replaced[0].context["run_id"] != hostile
    assert all("segredo-no-run-id" not in r.message + str(r.context) for r in logger.records)


def test_id_repetido_entre_dois_agentes_fica_com_o_primeiro():
    logger = RecordingLogger()
    first = Agent(id="dup", name="1", model=FakeChatModel(responses=["primeiro"]), telemetry=False)
    second = Agent(id="dup", name="2", model=FakeChatModel(responses=["segundo"]), telemetry=False)

    response = _client([first, second], logger=logger).post("/agui/dup", json=_body())

    assert "primeiro" in response.text and "segundo" not in response.text
    assert [r.message for r in logger.records if r.level == "warning"] == [
        "Id de entidade repetido; AG-UI expõe só a primeira"
    ]
