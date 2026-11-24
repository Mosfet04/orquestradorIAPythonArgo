"""QA do F4-01: smoke do app real com o transporte guardado de verdade (sem trocar o ``httpx.AsyncClient``).

Pilha: ``AppFactory`` (lifespan) -> ``DependencyContainer`` (config em YAML) -> ``HttpToolFactory`` sobre o
``GuardedAsyncTransport`` de produção -> pool do httpcore -> backend de rede falso (no lugar do anyio, só
dentro do teste). O DNS é falso: ``api.example.invalid`` é público, ``interno.example.invalid`` resolve para
``10.0.0.5``, ``rebind.example.invalid`` muda de público para metadata na segunda resolução. O que se confere é
o que o app faz com a guarda ligada: tool pública sai pelo IP conferido (Host pelo nome, segredo do header por
referência no fio), tool de rede interna fora da ``TOOL_ROUTE_ALLOWLIST`` é recusada na chamada (documento
legado) sem abrir TCP nem vazar o host/segredo no resultado ao modelo, e o shutdown fecha o pool.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpcore
import pytest
from agno.models.response import ModelResponse
from starlette.testclient import TestClient

from src.infrastructure import dependency_injection as di
from src.infrastructure.security.guarded_http import GuardedAsyncTransport
from src.infrastructure.web import app_factory
from tests.fakes import FakeChatModel
from tests.fakes.dns import PUBLIC_IP
from tests.fakes.network_backend import RecordingAsyncBackend, http_response
from tests.fakes.startup_world import CLEAN_ENV, Events, startup_world
from tests.fakes.web import LOOPBACK_BASE_URL, LOOPBACK_CLIENT

RUN_KEY = "qaf401-run-key-" + "r" * 21
ADMIN_KEY = "qaf401-admin-key-" + "a" * 19
RUN = {"Authorization": f"Bearer {RUN_KEY}"}
SECRET = "tok-qaf401-valor-que-nunca-sai"  # noqa: S105 - marcador de vazamento, não é segredo
SECRET_ENV = "QAF401_API_KEY"  # noqa: S105 - nome da variável de teste

CONFIG = """
agents:
  - id: ysol
    nome: Agente do YAML
    factoryIaModel: ollama
    model: m-ysol
    descricao: d
    prompt: [Você é um agente de teste.]
    tools_ids: [cep, interno, rebind]
    active: true
tools:
  - id: cep
    name: CEP
    description: Busca endereço pelo CEP
    route: https://api.example.invalid/cep/{cep}
    http_method: GET
    headers: {X-Token: "env:QAF401_API_KEY"}
    parameters:
      - {name: cep, type: string, description: CEP, required: true}
    active: true
  - id: interno
    name: Interno
    description: Rede interna (documento legado fora da allowlist)
    route: http://interno.example.invalid/dados
    http_method: GET
    headers: {X-Token: "env:QAF401_API_KEY"}
    parameters: []
    active: true
  - id: rebind
    name: Rebind
    description: Host que muda de IP
    route: https://rebind.example.invalid/x
    http_method: GET
    parameters: []
    active: true
"""


def _tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> ModelResponse:
    return ModelResponse(
        role="assistant",
        tool_calls=[
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
        ],
    )


class _Dns:
    def __init__(self) -> None:
        self.calls: list[str] = []
        self.rebind = [PUBLIC_IP, "169.254.169.254"]

    def __call__(self, host: str) -> list[str]:
        self.calls.append(host)
        if host == "interno.example.invalid":
            return ["10.0.0.5"]
        if host == "rebind.example.invalid":
            return [self.rebind.pop(0) if len(self.rebind) > 1 else self.rebind[0]]
        return [PUBLIC_IP]


class _Smoke:
    def __init__(self, http: TestClient, backend: RecordingAsyncBackend, dns: _Dns, model: FakeChatModel) -> None:
        self.http = http
        self.backend = backend
        self.dns = dns
        self.model = model
        self.tool_transports: list[GuardedAsyncTransport] = []  # os criados pelo composition root, para as tools
        self.transport_closed: list[GuardedAsyncTransport] = []  # os que tiveram ``aclose`` até o fim do shutdown


@contextmanager
def _smoke_app(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, script: list[Any]) -> Iterator[_Smoke]:
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
        SECRET_ENV: SECRET,
    }.items():
        monkeypatch.setenv(name, value)

    model = FakeChatModel(id="m-ysol", responses=script)
    backend = RecordingAsyncBackend(http_response(200, b'{"logradouro": "Praca da Se"}'))
    dns = _Dns()

    def create_model(self: Any, config: Any) -> FakeChatModel:
        return model

    created: list[GuardedAsyncTransport] = []
    closed: list[GuardedAsyncTransport] = []

    class SpyTransport(GuardedAsyncTransport):
        """Só o transporte que o composition root monta para as tools (o dos modelos é o puro)."""

        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            created.append(self)

        async def aclose(self) -> None:
            closed.append(self)
            await super().aclose()

    with (
        startup_world(Events(), agents=()),
        patch.object(di.ProviderRegistry, "create_model", create_model),
        patch.object(di, "system_resolver", dns),
        patch.object(httpcore, "AnyIOBackend", lambda: backend),
        patch.object(di, "GuardedAsyncTransport", SpyTransport),
    ):
        app = app_factory.AppFactory().create_app()
        with TestClient(app, base_url=LOOPBACK_BASE_URL, client=LOOPBACK_CLIENT, raise_server_exceptions=False) as http:
            smoke = _Smoke(http, backend, dns, model)
            yield smoke
    smoke.tool_transports = created
    smoke.transport_closed = closed


@pytest.mark.usefixtures("offline_knowledge")
def test_tool_publica_sai_pelo_ip_conferido_interna_e_recusada_e_o_shutdown_fecha_o_pool(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    script = [
        _tool_call("c1", "cep", {"cep": "01001000"}),
        _tool_call("c2", "interno", {}),
        _tool_call("c3", "rebind", {}),
        _tool_call("c4", "rebind", {}),
        "fim",
    ]
    with _smoke_app(monkeypatch, tmp_path, script) as smoke:
        run = smoke.http.post("/agents/ysol/runs", data={"message": "oi", "stream": "false"}, headers=RUN)
        wire = smoke.backend.sent()
        connects = list(smoke.backend.connects)
        dns_calls = list(smoke.dns.calls)
        # a última chamada ao modelo leva o histórico inteiro: um resultado de tool por chamada da tool
        tool_results = [str(content) for role, content in smoke.model.calls[-1].messages if role == "tool"]

    assert run.status_code == 200, run.text
    assert run.json()["content"] == "fim"
    # só a tool pública (e a 1ª chamada do rebind) abriram TCP, no IP conferido; a interna e o rebind não
    assert connects == [(PUBLIC_IP, 443), (PUBLIC_IP, 443)]
    assert b"GET /cep/01001000 HTTP/1.1" in wire and b"Host: api.example.invalid\r\n" in wire
    assert f"X-Token: {SECRET}".encode() in wire  # a referência foi resolvida e foi ao destino público
    assert wire.count(SECRET.encode()) == 1  # e só a ele: nem à tool interna, nem ao rebind
    assert any("Praca da Se" in content for content in tool_results)
    assert sum("destino recusado pela guarda de rede" in content for content in tool_results) == 2
    assert dns_calls == [
        "api.example.invalid",
        "interno.example.invalid",
        "rebind.example.invalid",  # 1ª chamada: a conexão confere o IP e abre (público)
        "rebind.example.invalid",  # 2ª chamada: a conexão confere de novo e recusa (metadata)
    ]
    for visible in (run.text, "\n".join(tool_results), caplog.text):
        assert SECRET not in visible
        assert "10.0.0.5" not in visible and "169.254.169.254" not in visible
    # um único transporte das tools, fechado exatamente uma vez no shutdown (o dos modelos não conta)
    assert len(smoke.tool_transports) == 1
    assert smoke.transport_closed == smoke.tool_transports
