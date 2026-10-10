"""QA do F2-07: smoke do app real depois de mover agui/cancel/tool HTTP/sumarização para ``runtime/agno``.

Pilha: ``AppFactory`` (lifespan) -> ``DependencyContainer`` (config em YAML) -> controller -> use cases ->
``AgnoRuntime`` -> AgentOS, sobre ``tests/fakes/startup_world.py`` (Mongo, db de sessões e modelo falsos),
em processo (``TestClient``, sem bind de porta, sem rede: a tool HTTP fala com um ``httpx.MockTransport``).
Aqui é uma fiação só: startup, ``/agents``, run REST com tool HTTP, run AG-UI, cancelamento, auth por API
key, CORS e as métricas de cache hit/miss que o composition root liga ao controller. Chaves são valores
de teste.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from agno.models.response import ModelResponse
from agno.run.cancel import get_cancellation_manager
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader
from starlette.testclient import TestClient

from src.infrastructure import dependency_injection as di
from src.infrastructure.telemetry import metrics as metrics_module
from src.infrastructure.web import app_factory
from tests.fakes import FakeChatModel
from tests.fakes.agui import assert_valid_run, parse_agui_sse, text_of, types_of
from tests.fakes.startup_world import CLEAN_ENV, Events, startup_world
from tests.fakes.web import LOOPBACK_BASE_URL, LOOPBACK_CLIENT

RUN_KEY = "qa7-run-key-" + "r" * 21
ADMIN_KEY = "qa7-admin-key-" + "a" * 19
RUN = {"Authorization": f"Bearer {RUN_KEY}"}
ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}
BASE = "https://api.example.invalid"
ALLOWED_ORIGIN = "https://painel.example.invalid"

CONFIG = f"""
agents:
  - id: ysol
    nome: Agente do YAML
    factoryIaModel: ollama
    model: m-ysol
    descricao: d
    prompt: [Você é um agente de teste.]
    tools_ids: [cep]
    active: true
  - id: lento
    nome: Agente lento
    factoryIaModel: ollama
    model: m-lento
    descricao: d
    prompt: [Você é um agente lento.]
    active: true
tools:
  - id: cep
    name: CEP
    description: Busca endereço pelo CEP
    route: {BASE}/cep/{{cep}}
    http_method: GET
    parameters:
      - {{name: cep, type: string, description: CEP, required: true}}
    active: true
"""


class _TickingModel(FakeChatModel):
    """Um trecho a cada 20 ms (até 500): o agno checa o cancelamento entre trechos."""

    async def ainvoke_stream(self, *args: Any, **kwargs: Any):  # type: ignore[no-untyped-def,override]
        for index in range(500):
            yield ModelResponse(role="assistant", content=f"t{index} ")
            await asyncio.sleep(0.02)


def _tool_call(call_id: str) -> ModelResponse:
    arguments = json.dumps({"cep": "01001000"})
    return ModelResponse(
        role="assistant",
        tool_calls=[{"id": call_id, "type": "function", "function": {"name": "cep", "arguments": arguments}}],
    )


class _Smoke:
    def __init__(self, http: TestClient, requests: list[httpx.Request], models: dict[str, FakeChatModel]) -> None:
        self.http = http
        self.requests = requests
        self.models = models


def _points(reader: InMemoryMetricReader) -> dict[str, list[tuple[str, int]]]:
    data = reader.get_metrics_data()
    return {
        metric.name: sorted((dict(p.attributes or {})["cache"], p.value) for p in metric.data.data_points)
        for resource in (data.resource_metrics if data else [])
        for scope in resource.scope_metrics
        for metric in scope.metrics
    }


@contextmanager
def smoke_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> Iterator[tuple[_Smoke, InMemoryMetricReader]]:
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
        "CORS_ALLOWED_ORIGINS": ALLOWED_ORIGIN,
    }.items():
        monkeypatch.setenv(name, value)

    reader = InMemoryMetricReader()
    meter_provider = MeterProvider(metric_readers=[reader])
    meter = meter_provider.get_meter("qa-f2-07")
    monkeypatch.setattr(metrics_module, "cache_hits_total", meter.create_counter("cache_hits_total"))
    monkeypatch.setattr(metrics_module, "cache_misses_total", meter.create_counter("cache_misses_total"))

    models: dict[str, FakeChatModel] = {
        "m-ysol": FakeChatModel(id="m-ysol", responses=[_tool_call("c1"), "resposta REST", _tool_call("c2"), "AGUI"]),
        "m-lento": _TickingModel(id="m-lento", responses=["x"]),
    }
    requests: list[httpx.Request] = []

    def create_model(self: Any, config: Any) -> FakeChatModel:
        return models[config.model_id]

    def transport_handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(200, json={"logradouro": "Praça da Sé"})

    real_client: Callable[..., httpx.AsyncClient] = httpx.AsyncClient

    def mocked_client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(transport_handler), **kwargs)

    try:
        with (
            startup_world(Events(), agents=()),
            patch.object(di.ProviderRegistry, "create_model", create_model),
            patch.object(httpx, "AsyncClient", mocked_client),
        ):
            app = app_factory.AppFactory().create_app()
            with TestClient(
                app, base_url=LOOPBACK_BASE_URL, client=LOOPBACK_CLIENT, raise_server_exceptions=False
            ) as http:
                yield _Smoke(http, requests, models), reader
    finally:
        meter_provider.shutdown()


def _agui_body(run_id: str) -> dict[str, Any]:
    return {
        "threadId": "t1", "runId": run_id, "state": {}, "tools": [], "context": [], "forwardedProps": {},
        "messages": [{"id": "m1", "role": "user", "content": "oi"}],
    }


@pytest.mark.usefixtures("offline_knowledge")
def test_startup_agents_run_rest_com_tool_http_agui_auth_e_cors(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with smoke_app(monkeypatch, tmp_path) as (smoke, _):
        http = smoke.http
        health = http.get("/admin/health", headers=ADMIN)
        listed = http.get("/agents", headers=RUN)
        rest = http.post("/agents/ysol/runs", data={"message": "oi", "stream": "false"}, headers=RUN)
        agui = http.post("/agui/ysol", json=_agui_body("r1"), headers=RUN)
        agui_missing = http.post("/agui/nao-existe", json=_agui_body("r2"), headers=RUN)
        no_key = http.post("/agents/ysol/runs", data={"message": "oi", "stream": "false"})
        run_key_on_admin = http.get("/admin/health", headers=RUN)
        allowed = http.options(
            "/agents",
            headers={"Origin": ALLOWED_ORIGIN, "Access-Control-Request-Method": "GET"},
        )
        denied = http.options(
            "/agents",
            headers={"Origin": "https://outro.example.invalid", "Access-Control-Request-Method": "GET"},
        )

    assert health.status_code == 200
    assert sorted(a["id"] for a in listed.json()) == ["lento", "ysol"]
    assert rest.status_code == 200, rest.text
    assert rest.json()["content"] == "resposta REST"
    events = parse_agui_sse(agui.text)
    assert_valid_run(events)
    assert text_of(events) == "AGUI"
    assert {"TOOL_CALL_START", "TOOL_CALL_RESULT"} <= set(types_of(events))
    # a tool HTTP (movida para runtime/agno) chamou o destino do YAML, uma vez por run
    assert [(r.method, str(r.url)) for r in smoke.requests] == [("GET", f"{BASE}/cep/01001000")] * 2
    assert any(
        role == "tool" and "Praça da Sé" in content for role, content in smoke.models["m-ysol"].calls[1].messages
    )
    assert agui_missing.status_code == 404
    assert no_key.status_code == 401
    assert run_key_on_admin.status_code == 403
    assert allowed.headers["access-control-allow-origin"] == ALLOWED_ORIGIN
    assert "access-control-allow-origin" not in denied.headers


@pytest.mark.usefixtures("offline_knowledge")
def test_cancelamento_de_run_rest_em_stream_pelo_router_do_runtime(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with smoke_app(monkeypatch, tmp_path) as (smoke, _):
        http = smoke.http
        manager = get_cancellation_manager()
        outcome: dict[str, httpx.Response] = {}

        def run_in_background() -> None:
            outcome["run"] = http.post("/agents/lento/runs", data={"message": "oi", "stream": "true"}, headers=RUN)

        worker = threading.Thread(target=run_in_background)
        worker.start()
        run_id = ""
        for _ in range(1000):
            active = list(manager.get_active_runs())
            if active:
                run_id = active[0]
                break
            threading.Event().wait(0.01)
        assert run_id, "o run não chegou a ser registrado no gerenciador"

        unknown_agent = http.post(f"/agents/nao-existe/runs/{run_id}/cancel", headers=RUN)
        unknown_run = http.post("/agents/lento/runs/nao-existe/cancel", headers=RUN)
        still_running = list(manager.get_active_runs())
        no_key = http.post(f"/agents/lento/runs/{run_id}/cancel")
        cancel = http.post(f"/agents/lento/runs/{run_id}/cancel", headers=RUN)
        worker.join(timeout=15)

    assert not worker.is_alive()
    assert no_key.status_code == 401
    assert unknown_agent.status_code == 404 and unknown_run.status_code == 404
    assert still_running == [run_id]  # as recusas não cancelaram o run
    assert cancel.status_code == 200
    assert "RunCancelled" in outcome["run"].text and run_id in outcome["run"].text
    assert manager.get_active_runs() == {}


@pytest.mark.usefixtures("offline_knowledge")
def test_metricas_de_cache_hit_e_miss_do_startup_e_do_refresh_chegam_ao_otel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """O controller não importa a telemetria: o composition root liga os callbacks; aqui, no app real."""
    with smoke_app(monkeypatch, tmp_path) as (smoke, reader):
        after_startup = _points(reader)
        refresh = smoke.http.post("/admin/refresh-cache", headers=ADMIN)
        after_refresh = _points(reader)

    # startup: warm_up (miss de agents; os teams são carregados direto) e depois get_agents e get_teams
    # do lifespan, ambos servidos pelo cache (hit); nenhum miss de teams
    assert after_startup == {"cache_misses_total": [("agents", 1)], "cache_hits_total": [("agents", 1), ("teams", 1)]}
    # o refresh recarrega e troca os caches sem passar por get_agents/get_teams: contadores iguais
    assert refresh.status_code == 200
    assert after_refresh == after_startup
