"""QA do F2-05: ciclo de vida do AgentOS com uvicorn real em 127.0.0.1 (lifespan ligado).

Pilha real de startup (``AppFactory`` -> container -> ``AgnoRuntime`` -> AgentOS) com só o Mongo, o
db de sessões e o modelo falsos (``tests/fakes/startup_world.py``). O servidor roda num subprocesso
(SIGTERM de verdade) sobre um socket pré-ligado, entregue ainda aberto ao uvicorn (sem janela de
corrida por porta entre workers do xdist).

Cobre: startup, run REST e AG-UI com chave, SIGTERM com run em andamento (runtime, telemetria e
cliente Mongo fecham uma vez, sem exceção nos logs), falha em cada etapa do startup (app recusa
subir e fecha o que abriu uma vez; ``mount`` mantém o comportamento de antes: sobe só com a borda),
``/livez`` durante startup lento e restart do lifespan no mesmo processo.
"""

from __future__ import annotations

import os
import signal
import socket
import subprocess
import sys
import threading
import time
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
from starlette.testclient import TestClient

from src.infrastructure.web.app_factory import AppFactory
from tests.fakes.startup_world import Events, clean_env, startup_world

ROOT = Path(__file__).resolve().parents[2]
RUN_KEY = "qa-f205-run-key-" + "r" * 21
ADMIN_KEY = "qa-f205-admin-key-" + "a" * 19
RUN_AUTH = {"Authorization": f"Bearer {RUN_KEY}"}
ADMIN_AUTH = {"Authorization": f"Bearer {ADMIN_KEY}"}
AGUI_BODY = {
    "threadId": "t1", "runId": "r1", "state": {}, "tools": [], "context": [], "forwardedProps": {},
    "messages": [{"id": "m1", "role": "user", "content": "oi"}],
}
# Marcas de exceção não tratada nos logs do uvicorn/asyncio/agno (INFO do uvicorn não as usa).
BAD_LOG_MARKS = ("Traceback", "Exception in ASGI application", "Task exception was never retrieved", "CancelledError")


class Child:
    """Subprocesso do app: lê o stdout/stderr (eventos ``EVT <nome>`` e logs) numa thread."""

    def __init__(self, process: subprocess.Popen[str], port: int) -> None:
        self.process = process
        self.port = port
        self.lines: list[str] = []
        self._cond = threading.Condition()
        self._reader = threading.Thread(target=self._read, daemon=True)
        self._reader.start()

    def _read(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            with self._cond:
                self.lines.append(line.rstrip("\n"))
                self._cond.notify_all()

    @property
    def base(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    def events(self) -> list[str]:
        with self._cond:
            return [ln[4:] for ln in self.lines if ln.startswith("EVT ")]

    def wait_event(self, name: str, timeout: float = 30) -> None:
        deadline = time.monotonic() + timeout
        with self._cond:
            while f"EVT {name}" not in self.lines:
                left = deadline - time.monotonic()
                if left <= 0 or (self.process.poll() is not None and not self._reader.is_alive()):
                    raise AssertionError(f"evento {name!r} não ocorreu; eventos={self.events()}")
                self._cond.wait(min(left, 0.2))

    def wait_ready(self, timeout: float = 40) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self.process.poll() is not None:
                raise AssertionError(f"app saiu no startup (código {self.process.returncode}):\n{self.log()}")
            try:
                if httpx.get(f"{self.base}/livez", timeout=2).status_code == 200:
                    return
            except httpx.TransportError:
                time.sleep(0.1)
        raise AssertionError(f"app não ficou pronto:\n{self.log()}")

    def exit_code(self, timeout: float = 30) -> int:
        code = self.process.wait(timeout=timeout)
        self._reader.join(timeout=10)
        return code

    def assert_graceful_exit(self, timeout: float = 30) -> None:
        """O uvicorn termina o shutdown e re-levanta o sinal capturado: saída 0 ou morte por SIGTERM."""
        code = self.exit_code(timeout)
        assert code in (0, -signal.SIGTERM), (code, self.log())

    def log(self) -> str:
        with self._cond:
            return "\n".join(self.lines)

    def non_event_log(self) -> str:
        with self._cond:
            return "\n".join(ln for ln in self.lines if not ln.startswith("EVT "))


@pytest.fixture
def spawn() -> Iterator[Callable[..., Child]]:
    children: list[tuple[Child, socket.socket]] = []

    def _spawn(*extra: str, keyed: bool = True) -> Child:
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        os.set_inheritable(listener.fileno(), True)
        env = clean_env()
        env.update(APP_HOST="127.0.0.1", ENVIRONMENT="production", CORS_ALLOWED_ORIGINS="https://painel.example.com")
        if keyed:
            env.update(API_KEY_RUN=RUN_KEY, API_KEY_ADMIN=ADMIN_KEY)
        process = subprocess.Popen(  # noqa: S603 - python do próprio venv, argumentos fixos do teste
            [sys.executable, "-m", "tests.fakes.startup_world", "--fd", str(listener.fileno()), *extra],
            cwd=ROOT,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            pass_fds=[listener.fileno()],
        )
        child = Child(process, int(listener.getsockname()[1]))
        children.append((child, listener))
        return child

    yield _spawn
    for child, listener in children:
        if child.process.poll() is None:
            child.process.kill()
            child.process.wait(timeout=10)
        listener.close()


def _assert_clean_log(child: Child) -> None:
    log = child.non_event_log()
    assert not [m for m in BAD_LOG_MARKS if m in log], log


# ── startup e runs com chave ─────────────────────────────────────────


def test_startup_real_run_rest_e_agui_com_chave_e_sigterm_gracioso(spawn: Callable[..., Child]) -> None:
    child = spawn()
    child.wait_ready()

    with httpx.Client(base_url=child.base, timeout=20) as http:
        assert http.get("/agents").status_code == 401
        assert http.post("/agents/a1/runs", data={"message": "oi", "stream": "false"}).status_code == 401
        listed = http.get("/agents", headers=RUN_AUTH)
        assert [a["id"] for a in listed.json()] == ["a1"]
        run = http.post("/agents/a1/runs", data={"message": "oi", "stream": "false"}, headers=RUN_AUTH)
        assert run.status_code == 200 and run.json()["content"] == "resposta do modelo"
        agui = http.post("/agui/a1", json=AGUI_BODY, headers=RUN_AUTH)
        assert agui.status_code == 200
        assert "RUN_FINISHED" in agui.text and "resposta do modelo" in agui.text
        # Stubs 503 sem db= e /approvals* só admin (F2-05).
        assert http.get("/approvals", headers=RUN_AUTH).status_code == 403
        assert http.get("/approvals", headers=ADMIN_AUTH).status_code == 503
        assert http.get("/components", headers=ADMIN_AUTH).status_code == 503

    child.process.send_signal(signal.SIGTERM)
    child.assert_graceful_exit()
    events = child.events()
    assert events[:2] == ["db:start", "http:start"]
    # Shutdown em ordem: flush da telemetria (antes do fechamento que pode demorar), runtime (lifespans
    # do agno: http, dbs), cliente Mongo; cada um uma vez.
    assert events[events.index("telemetry:shutdown"):] == [
        "telemetry:shutdown", "runtime:close", "http:stop", "db:close", "db:close", "db:stop", "motor:close",
    ]
    assert "db:provisioned" not in events
    _assert_clean_log(child)


def test_sigterm_com_run_rest_em_andamento_conclui_o_run_e_fecha_cada_recurso_uma_vez(
    spawn: Callable[..., Child],
) -> None:
    child = spawn("--run-delay", "2")
    child.wait_ready()
    result: dict[str, httpx.Response] = {}

    def long_run() -> None:
        result["r"] = httpx.post(
            f"{child.base}/agents/a1/runs", data={"message": "oi", "stream": "false"}, headers=RUN_AUTH, timeout=30
        )

    worker = threading.Thread(target=long_run)
    worker.start()
    child.wait_event("model:start")
    child.process.send_signal(signal.SIGTERM)
    worker.join(timeout=30)

    assert result["r"].status_code == 200 and result["r"].json()["content"] == "resposta do modelo"
    child.assert_graceful_exit()
    events = child.events()
    assert [events.count(n) for n in ("runtime:close", "telemetry:shutdown", "motor:close")] == [1, 1, 1]
    assert events.index("model:start") < events.index("runtime:close")  # o run terminou antes de fechar
    _assert_clean_log(child)


def test_sigterm_com_run_agui_em_streaming_conclui_o_stream_e_fecha_uma_vez(spawn: Callable[..., Child]) -> None:
    child = spawn("--run-delay", "2")
    child.wait_ready()
    result: dict[str, httpx.Response] = {}

    def long_stream() -> None:
        result["r"] = httpx.post(f"{child.base}/agui/a1", json=AGUI_BODY, headers=RUN_AUTH, timeout=30)

    worker = threading.Thread(target=long_stream)
    worker.start()
    child.wait_event("model:start")
    child.process.send_signal(signal.SIGTERM)
    worker.join(timeout=30)

    assert result["r"].status_code == 200 and "RUN_FINISHED" in result["r"].text
    child.assert_graceful_exit()
    events = child.events()
    assert [events.count(n) for n in ("runtime:close", "telemetry:shutdown", "motor:close")] == [1, 1, 1]
    _assert_clean_log(child)


def test_sigterm_com_timeout_gracioso_menor_que_o_run_ainda_fecha_uma_vez(spawn: Callable[..., Child]) -> None:
    child = spawn("--run-delay", "8", "--graceful-timeout", "1")
    child.wait_ready()
    result: dict[str, object] = {}

    def long_run() -> None:
        try:
            result["r"] = httpx.post(
                f"{child.base}/agents/a1/runs", data={"message": "oi", "stream": "false"},
                headers=RUN_AUTH, timeout=30,
            )
        except httpx.TransportError as exc:
            result["r"] = exc

    worker = threading.Thread(target=long_run)
    worker.start()
    child.wait_event("model:start")
    started = time.monotonic()
    child.process.send_signal(signal.SIGTERM)
    child.assert_graceful_exit()
    worker.join(timeout=30)

    assert time.monotonic() - started < 7  # não esperou o run de 8 s
    events = child.events()
    assert [events.count(n) for n in ("runtime:close", "telemetry:shutdown", "motor:close")] == [1, 1, 1]
    assert not [m for m in ("Exception in ASGI application", "Task exception was never retrieved")
                if m in child.non_event_log()], child.non_event_log()


# ── falha em cada etapa do startup ───────────────────────────────────


@pytest.mark.parametrize(
    ("step", "runtime_closes", "agno_lifespans"),
    [("container", 0, []), ("entities", 0, []), ("start", 1, ["db:start", "http:start"])],
)
def test_falha_no_startup_recusa_subir_e_fecha_o_que_abriu_uma_vez(
    spawn: Callable[..., Child], step: str, runtime_closes: int, agno_lifespans: list[str]
) -> None:
    child = spawn("--fail-at", step)

    code = child.exit_code()

    assert code != 0, child.log()
    events = child.events()
    assert f"falha injetada: {step}" in child.non_event_log()
    assert [events.count(n) for n in ("motor:close", "telemetry:shutdown")] == [1, 1]
    assert events.count("runtime:close") == runtime_closes
    assert [e for e in events if e in ("db:start", "http:start", "http:stop", "db:stop")] == agno_lifespans
    # O app nunca atendeu: o socket só entra em listen depois do startup do lifespan.
    with pytest.raises(httpx.TransportError):
        httpx.get(f"{child.base}/livez", timeout=2)


def test_falha_ao_montar_o_runtime_segue_logada_como_antes_e_fecha_uma_vez(spawn: Callable[..., Child]) -> None:
    """Como antes do F2-05: a montagem falha logada e o app segue (não recusa o startup).

    A falha injetada vem depois do ``get_app()`` (router AG-UI): as rotas do AgentOS já estão no
    app (montagem parcial, igual à base ``811d082``), o AG-UI não; o runtime não marca montagem,
    então ``start`` não abre os lifespans do AgentOS e ``close`` não tem o que fechar.
    """
    child = spawn("--fail-at", "mount")
    child.wait_ready()

    with httpx.Client(base_url=child.base, timeout=20) as http:
        assert http.get("/livez").status_code == 200
        assert http.get("/agents", headers=RUN_AUTH).status_code == 200  # rotas do AgentOS (parcial)
        assert http.post("/agui/a1", json=AGUI_BODY, headers=RUN_AUTH).status_code == 404  # AG-UI não montou
        assert http.get("/admin/health", headers=ADMIN_AUTH).status_code == 200

    child.process.send_signal(signal.SIGTERM)
    child.assert_graceful_exit()
    events = child.events()
    log = child.non_event_log()
    assert "Erro ao montar AgentOS" in log and "RuntimeError" in log
    assert "falha injetada: mount" not in log  # F2-07 (R3): o log leva o tipo do erro, nunca o texto
    assert [events.count(n) for n in ("runtime:close", "telemetry:shutdown", "motor:close")] == [1, 1, 1]
    # Montagem não concluída: start não abre lifespan nenhum e close não tem o que fechar.
    assert not [e for e in events if e in ("db:start", "http:start", "http:stop", "db:stop", "db:close")]


# ── /livez durante startup lento ─────────────────────────────────────


def test_livez_durante_startup_lento_nunca_responde_errado_e_fica_200_ao_fim(spawn: Callable[..., Child]) -> None:
    """Comportamento (igual ao de antes): o uvicorn só escuta depois do lifespan, então a sonda
    recebe recusa de conexão durante o startup e 200 depois; nunca 5xx nem resposta pendurada."""
    child = spawn("--startup-delay", "3")
    probes: list[str] = []
    deadline = time.monotonic() + 40
    while time.monotonic() < deadline:
        assert child.process.poll() is None, child.log()
        try:
            response = httpx.get(f"{child.base}/livez", timeout=2)
        except httpx.TransportError:
            probes.append("recusado")
            time.sleep(0.2)
            continue
        probes.append(str(response.status_code))
        break

    assert probes[-1] == "200", probes
    assert set(probes[:-1]) <= {"recusado"}, probes
    assert "recusado" in probes  # o startup de 3 s foi observado como indisponível, não como 200
    child.process.send_signal(signal.SIGTERM)
    child.assert_graceful_exit()


def test_sigterm_durante_o_startup_lento_termina_o_startup_e_fecha_tudo_uma_vez(spawn: Callable[..., Child]) -> None:
    child = spawn("--startup-delay", "3")
    deadline = time.monotonic() + 30
    while not [ln for ln in child.lines if "Waiting for application startup" in ln]:
        assert time.monotonic() < deadline and child.process.poll() is None, child.log()
        time.sleep(0.05)
    child.process.send_signal(signal.SIGTERM)

    child.assert_graceful_exit()

    events = child.events()
    assert [events.count(n) for n in ("runtime:close", "telemetry:shutdown", "motor:close")] == [1, 1, 1]
    assert events.index("db:start") < events.index("runtime:close")  # o startup terminou antes do shutdown
    assert [events.count(n) for n in ("db:start", "db:stop", "http:start", "http:stop")] == [1, 1, 1, 1]
    _assert_clean_log(child)


# ── restart do lifespan no mesmo processo ────────────────────────────

LOCAL = {"base_url": "http://127.0.0.1:7777", "client": ("127.0.0.1", 50000)}


def test_lifespan_duas_vezes_no_mesmo_app_e_recusado_sem_reabrir_nem_fechar_de_novo(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """BUG-F2-05-QA-1: o 2º lifespan do mesmo app é recusado com erro claro (o 1º shutdown fechou o
    container e os dbs das entidades para os quais as rotas montadas apontam); nada reabre nem fecha
    de novo."""
    for name in ("API_KEY_RUN", "API_KEY_ADMIN", "APP_HOST", "ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    monkeypatch.setenv("OTEL_ENABLED", "false")
    events = Events()
    with startup_world(events, agents=("a1",), teams={"t1": ["a1"]}):
        app = AppFactory().create_app()
        with TestClient(app, **LOCAL) as http:  # type: ignore[arg-type]
            assert [a["id"] for a in http.get("/agents").json()] == ["a1"]
            assert http.post("/agents/a1/runs", data={"message": "oi", "stream": "false"}).status_code == 200
        with pytest.raises(RuntimeError, match="Lifespan já executado"):
            with TestClient(app, **LOCAL):  # type: ignore[arg-type]
                pass  # pragma: no cover - startup recusado

    assert events.names.count("db:start") == events.names.count("db:stop") == 1
    assert events.names.count("http:start") == events.names.count("http:stop") == 1
    assert events.names.count("runtime:close") == 1
    assert events.names.count("db:close") == 2  # agente a1 e team t1, uma vez cada
    assert not [e for e in events.names if e == "db:provisioned"]


def test_restart_do_lifespan_nunca_perde_a_sessao_em_silencio(monkeypatch: pytest.MonkeyPatch) -> None:
    """Intenção do BUG-F2-05-QA-1: um run depois do 1º shutdown nunca grava num db fechado sem falha
    visível; o 2º lifespan é recusado antes de servir."""
    for name in ("API_KEY_RUN", "API_KEY_ADMIN", "APP_HOST", "ENVIRONMENT"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    monkeypatch.setenv("OTEL_ENABLED", "false")
    events = Events()
    with startup_world(events, agents=("a1",), teams={"t1": ["a1"]}):
        app = AppFactory().create_app()
        with TestClient(app, **LOCAL) as http:  # type: ignore[arg-type]
            assert http.post("/agents/a1/runs", data={"message": "oi", "stream": "false"}).status_code == 200
        with pytest.raises(RuntimeError, match="crie um AppFactory novo"):
            with TestClient(app, **LOCAL):  # type: ignore[arg-type]
                pass  # pragma: no cover - startup recusado

    assert "db:use-after-close" not in events.names
