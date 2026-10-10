"""Bordas do MetricsMiddleware (QA F1-01): corpo vazio, headers ausentes, ASGI atípico, concorrência."""

from __future__ import annotations

import asyncio
import json

import pytest
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from src.infrastructure.web.metrics_middleware import MetricsMiddleware
from tests.fakes.telemetry import RecordingTelemetryMetrics

AGENT_PATH = "/agents/agente-1/runs"
TEAM_PATH = "/teams/time-1/runs"


def _sse(event: str) -> bytes:
    return f'event: {event}\ndata: {{"event":"{event}"}}\n\n'.encode()


async def _run(
    app: ASGIApp, metrics: RecordingTelemetryMetrics, path: str = AGENT_PATH, method: str = "POST"
) -> list[Message]:
    sent: list[Message] = []

    async def receive() -> Message:
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message: Message) -> None:
        sent.append(message)

    await MetricsMiddleware(app, metrics=metrics)({"type": "http", "method": method, "path": path}, receive, send)
    return sent


def _messages_app(*messages: Message) -> ASGIApp:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        for message in messages:
            await send(message)

    return app


def _start(status: int = 200, content_type: bytes | None = b"application/json") -> Message:
    headers = [] if content_type is None else [(b"content-type", content_type)]
    return {"type": "http.response.start", "status": status, "headers": headers}


@pytest.fixture
def metrics() -> RecordingTelemetryMetrics:
    return RecordingTelemetryMetrics()


# ── corpo e headers atípicos ────────────────────────────────────────


def test_json_com_corpo_vazio_nao_conta_erro_nem_quebra(metrics: RecordingTelemetryMetrics) -> None:
    app = _messages_app(_start(), {"type": "http.response.body", "body": b""})

    sent = asyncio.run(_run(app, metrics))

    assert [m["type"] for m in sent] == ["http.response.start", "http.response.body"]
    assert metrics.agent_errors == {}
    assert metrics.request_statuses == {("agente-1", "success"): 1}


def test_resposta_sem_content_type_nao_e_inspecionada(metrics: RecordingTelemetryMetrics) -> None:
    body = json.dumps({"status": "ERROR"}).encode()
    app = _messages_app(_start(content_type=None), {"type": "http.response.body", "body": body})

    asyncio.run(_run(app, metrics))

    assert metrics.agent_errors == {}
    assert metrics.request_statuses == {("agente-1", "success"): 1}


def test_body_sem_more_body_equivale_a_ultimo_chunk(metrics: RecordingTelemetryMetrics) -> None:
    app = _messages_app(_start(), {"type": "http.response.body", "body": b'{"status":"ERROR"}'})  # sem more_body

    asyncio.run(_run(app, metrics))

    assert metrics.agent_errors == {"agente-1": 1}


def test_body_sem_chave_body_nao_quebra(metrics: RecordingTelemetryMetrics) -> None:
    app = _messages_app(_start(), {"type": "http.response.body"})

    asyncio.run(_run(app, metrics))

    assert metrics.agent_errors == {}
    assert metrics.active == {"agente-1": 0}


def test_content_type_em_maiusculas_e_charset_sao_reconhecidos(metrics: RecordingTelemetryMetrics) -> None:
    app = _messages_app(
        {
            "type": "http.response.start",
            "status": 200,
            "headers": [(b"Content-Type", b"Application/JSON; charset=utf-8")],
        },
        {"type": "http.response.body", "body": b'{"status":"ERROR"}'},
    )

    asyncio.run(_run(app, metrics))

    assert metrics.agent_errors == {"agente-1": 1}


def test_start_sem_headers_nao_quebra(metrics: RecordingTelemetryMetrics) -> None:
    app = _messages_app({"type": "http.response.start", "status": 200}, {"type": "http.response.body", "body": b"x"})

    asyncio.run(_run(app, metrics))

    assert metrics.agent_requests == {"agente-1": 1}
    assert metrics.agent_errors == {}


def test_corpo_json_de_aninhamento_extremo_nao_derruba_a_resposta(metrics: RecordingTelemetryMetrics) -> None:
    """O observador nunca pode quebrar a resposta: JSON profundo (RecursionError) é só ignorado."""
    body = b"[" * 100_000 + b"]" * 100_000
    app = _messages_app(_start(), {"type": "http.response.body", "body": body})

    sent = asyncio.run(_run(app, metrics))

    assert sent[-1]["body"] == body
    assert metrics.active == {"agente-1": 0}
    assert metrics.agent_errors == {}


def test_varios_http_response_start_nao_duplicam_erro(metrics: RecordingTelemetryMetrics) -> None:
    app = _messages_app(
        _start(500),
        _start(500),
        {"type": "http.response.body", "body": b"x"},
    )

    asyncio.run(_run(app, metrics))

    assert metrics.agent_errors == {"agente-1": 1}
    assert metrics.agent_requests == {"agente-1": 1}
    assert metrics.active == {"agente-1": 0}


@pytest.mark.parametrize("method", ["GET", "HEAD", "OPTIONS", "PUT", "DELETE", "post"])
@pytest.mark.parametrize("path", [AGENT_PATH, TEAM_PATH])
def test_outros_metodos_na_rota_de_run_nao_contam(metrics: RecordingTelemetryMetrics, method: str, path: str) -> None:
    seen: list[str] = []

    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        seen.append(scope["method"])
        await send(_start(405))
        await send({"type": "http.response.body", "body": b""})

    asyncio.run(_run(app, metrics, path=path, method=method))

    assert seen == [method]
    assert (metrics.agent_requests, metrics.team_requests, metrics.agent_errors, metrics.team_errors) == ({},) * 4
    assert metrics.active == {}


@pytest.mark.parametrize(
    "path",
    [
        "/agents/runs",
        "/agents//runs",
        "/agents/a/b/runs",
        "/agents/agente-1/runs/extra",
        "/xplayground/agents/agente-1/runs",
        "/agents/agente-1/runsx",
    ],
)
def test_paths_parecidos_nao_sao_run(metrics: RecordingTelemetryMetrics, path: str) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send(_start(404))
        await send({"type": "http.response.body", "body": b""})

    asyncio.run(_run(app, metrics, path=path))

    assert (metrics.agent_requests, metrics.team_requests, metrics.active) == ({}, {}, {})


def test_team_com_excecao_conta_um_erro_de_team(metrics: RecordingTelemetryMetrics) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise RuntimeError("x")

    with pytest.raises(RuntimeError):
        asyncio.run(_run(app, metrics, path=TEAM_PATH))

    assert metrics.team_errors == {"time-1": 1}
    assert metrics.request_statuses == {("time-1", "error"): 1}
    assert metrics.agent_errors == {}
    assert metrics.active == {}


# ── SSE: evento partido em qualquer ponto ───────────────────────────


@pytest.mark.parametrize("event", ["RunError", "TeamRunError"])
def test_evento_de_erro_detectado_em_qualquer_ponto_de_corte(event: str) -> None:
    stream = _sse("RunStarted") + _sse(event) + _sse("RunCompleted")
    path = AGENT_PATH if event == "RunError" else TEAM_PATH
    for cut in range(1, len(stream)):
        metrics = RecordingTelemetryMetrics()
        app = _messages_app(
            _start(200, b"text/event-stream"),
            {"type": "http.response.body", "body": stream[:cut], "more_body": True},
            {"type": "http.response.body", "body": stream[cut:], "more_body": False},
        )
        asyncio.run(_run(app, metrics, path=path))
        errors = metrics.agent_errors if event == "RunError" else metrics.team_errors
        assert sum(errors.values()) == 1, f"corte em {cut}"


def test_sse_byte_a_byte_conta_um_erro(metrics: RecordingTelemetryMetrics) -> None:
    stream = _sse("RunStarted") + _sse("RunError")
    messages = [_start(200, b"text/event-stream")]
    messages += [
        {"type": "http.response.body", "body": stream[i : i + 1], "more_body": True} for i in range(len(stream))
    ]
    messages.append({"type": "http.response.body", "body": b"", "more_body": False})

    asyncio.run(_run(_messages_app(*messages), metrics))

    assert metrics.agent_errors == {"agente-1": 1}


def test_sse_erro_so_no_ultimo_chunk_sem_newline_final_nao_conta(metrics: RecordingTelemetryMetrics) -> None:
    """Marca exige '\\n' final: 'event: RunErrorX' (outro evento) não pode contar."""
    app = _messages_app(
        _start(200, b"text/event-stream"),
        {"type": "http.response.body", "body": b"event: RunErrorX\ndata: {}\n\n"},
    )

    asyncio.run(_run(app, metrics))

    assert metrics.agent_errors == {}


def test_sse_com_erro_e_depois_excecao_conta_um_unico_erro(metrics: RecordingTelemetryMetrics) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        await send(_start(200, b"text/event-stream"))
        await send({"type": "http.response.body", "body": _sse("RunError"), "more_body": True})
        raise RuntimeError("cliente caiu")

    with pytest.raises(RuntimeError):
        asyncio.run(_run(app, metrics))

    assert metrics.agent_errors == {"agente-1": 1}
    assert metrics.request_statuses == {("agente-1", "error"): 1}


# ── concorrência e cancelamento real ────────────────────────────────


def test_cancelamento_de_task_em_voo_libera_active(metrics: RecordingTelemetryMetrics) -> None:
    """Cancela a task enquanto o app aguarda (cliente desconectou): gauge volta a 0, sem erro."""

    async def scenario() -> None:
        started = asyncio.Event()

        async def app(scope: Scope, receive: Receive, send: Send) -> None:
            await send(_start(200, b"text/event-stream"))
            started.set()
            await asyncio.sleep(60)

        task = asyncio.create_task(_run(app, metrics))
        await started.wait()
        assert metrics.active == {"agente-1": 1}  # em voo
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    asyncio.run(scenario())

    assert metrics.active == {"agente-1": 0}
    assert metrics.agent_errors == {}
    assert metrics.request_statuses == {("agente-1", "success"): 1}


def test_runs_concorrentes_com_falha_e_cancelamento_zeram_active(metrics: RecordingTelemetryMetrics) -> None:
    async def scenario() -> None:
        gate = asyncio.Event()

        async def ok(scope: Scope, receive: Receive, send: Send) -> None:
            await gate.wait()
            await send(_start())
            await send({"type": "http.response.body", "body": b'{"status":"COMPLETED"}'})

        async def erro(scope: Scope, receive: Receive, send: Send) -> None:
            await gate.wait()
            raise RuntimeError("x")

        async def trava(scope: Scope, receive: Receive, send: Send) -> None:
            await asyncio.sleep(60)

        tasks = [asyncio.create_task(_run(a, metrics)) for a in [ok] * 5 + [erro] * 3 + [trava] * 4]
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert metrics.active == {"agente-1": 12}
        for task in tasks[-4:]:
            task.cancel()
        gate.set()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        assert sum(isinstance(r, asyncio.CancelledError) for r in results) == 4
        assert sum(isinstance(r, RuntimeError) for r in results) == 3

    asyncio.run(scenario())

    assert metrics.active == {"agente-1": 0}
    assert metrics.agent_errors == {"agente-1": 3}
    assert metrics.agent_requests == {"agente-1": 12}


def test_cancelamento_de_team_nao_toca_agents_active(metrics: RecordingTelemetryMetrics) -> None:
    async def app(scope: Scope, receive: Receive, send: Send) -> None:
        raise asyncio.CancelledError

    with pytest.raises(asyncio.CancelledError):
        asyncio.run(_run(app, metrics, path=TEAM_PATH))

    assert metrics.active == {}
    assert metrics.team_errors == {}
    assert metrics.request_statuses == {("time-1", "success"): 1}


def test_estado_nao_vaza_entre_requests(metrics: RecordingTelemetryMetrics) -> None:
    """Uma instância do middleware atende N requests: falha de uma não contamina a seguinte."""

    async def scenario() -> None:
        failing = _messages_app(_start(), {"type": "http.response.body", "body": b'{"status":"ERROR"}'})
        fine = _messages_app(_start(), {"type": "http.response.body", "body": b'{"status":"COMPLETED"}'})
        for app in (failing, fine):
            await _run(app, metrics)

    asyncio.run(scenario())

    assert metrics.agent_errors == {"agente-1": 1}
    assert metrics.request_statuses == {("agente-1", "error"): 1, ("agente-1", "success"): 1}

    # e com a MESMA instância
    shared = MetricsMiddleware(
        _messages_app(_start(), {"type": "http.response.body", "body": b'{"status":"COMPLETED"}'}), metrics=metrics
    )

    async def twice() -> None:
        for _ in range(2):
            await shared(
                {"type": "http", "method": "POST", "path": AGENT_PATH},
                lambda: asyncio.sleep(0, {"type": "http.request"}),  # type: ignore[arg-type,return-value]
                _discard,
            )

    asyncio.run(twice())
    assert metrics.agent_errors == {"agente-1": 1}


async def _discard(message: Message) -> None:
    return None
