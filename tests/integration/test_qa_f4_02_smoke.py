"""QA do F4-02: smoke do app real (lifespan) com uma tool MCP de verdade, sem rede nem LLM.

Pilha: ``AppFactory`` (lifespan) -> ``DependencyContainer`` (config em YAML) -> ``ToolFactoryBySource`` ->
``McpToolFactory`` -> ``McpSessions`` de produção -> SDK ``mcp`` -> ``FakeMcpServer`` (SDK em processo, atrás de um
``httpx.ASGITransport``). Só o transporte é trocado (``transport_factory`` do composition root), porque o fake não
tem socket; o resto do caminho é o de produção. O modelo é roteirizado.

Confere: a sessão abre no startup (``initialize`` + ``tools/list`` antes de qualquer request ao app), o modelo só vê a
tool da allowlist, a chamada chega ao servidor com o header resolvido da referência, o segredo não aparece em log nem
na resposta, e o shutdown do lifespan fecha a sessão (DELETE) sem task dona viva.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from agno.models.response import ModelResponse
from starlette.testclient import TestClient

from src.infrastructure import dependency_injection as di
from src.infrastructure.mcp_client.sessions import McpSessions, schema_hash
from src.infrastructure.web import app_factory
from tests.fakes import FakeChatModel
from tests.fakes.mcp_server import FakeMcpServer, mcp_tool
from tests.fakes.startup_world import CLEAN_ENV, Events, startup_world
from tests.fakes.web import LOOPBACK_BASE_URL, LOOPBACK_CLIENT

RUN_KEY = "qaf402-run-key-" + "r" * 21
ADMIN_KEY = "qaf402-admin-key-" + "a" * 19
RUN = {"Authorization": f"Bearer {RUN_KEY}"}
LEAK_MARKER = "tok-qaf402-valor-que-nunca-sai"  # marcador de vazamento, não é segredo
REF_ENV_NAME = "QAF402_API_KEY"  # nome da variável de teste

SEARCH = mcp_tool("search", "Busca issues", {"q": {"type": "string"}})
DELETE_REPO = mcp_tool("delete_repo", "Apaga o repositório")
SEARCH_HASH = schema_hash(SEARCH.name, SEARCH.description, SEARCH.inputSchema)

CONFIG = f"""
agents:
  - id: ysol
    nome: Agente do YAML
    factoryIaModel: ollama
    model: m-ysol
    descricao: d
    prompt: [Você é um agente de teste.]
    tools_ids: [gh, outro]
    active: true
tools:
  - id: gh
    name: GitHub
    description: Servidor MCP
    route: https://mcp.example.com/mcp
    http_method: GET
    headers: {{Authorization: "env:{REF_ENV_NAME}"}}
    mcp_tools: {{search: "{SEARCH_HASH}"}}
    active: true
  - id: outro
    name: Outro
    description: Servidor MCP com schema que não bate
    route: https://mcp.example.com/mcp
    http_method: GET
    mcp_tools: {{delete_repo: "{'0' * 64}"}}
    active: true
"""


def _tool_call(call_id: str, name: str, arguments: dict[str, Any]) -> ModelResponse:
    return ModelResponse(
        role="assistant",
        tool_calls=[
            {"id": call_id, "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
        ],
    )


class _Smoke:
    def __init__(self, http: TestClient, server: FakeMcpServer, model: FakeChatModel) -> None:
        self.http = http
        self.server = server
        self.model = model
        self.methods_after_startup: list[str] = []
        self.open_after_shutdown: list[str] = []


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
        "TOOL_ROUTE_ALLOWLIST": "mcp.example.com",  # header por referência só vai a host do operador
        REF_ENV_NAME: LEAK_MARKER,
    }.items():
        monkeypatch.setenv(name, value)

    server = FakeMcpServer([SEARCH, DELETE_REPO], stateless=False)
    model = FakeChatModel(id="m-ysol", responses=script)

    class SessionsOnFakeServer(McpSessions):
        """McpSessions de produção; o fake sobe no event loop do app (o do TestClient) e cai junto com ele."""

        def __init__(self, logger: Any, **kwargs: Any) -> None:
            kwargs["transport_factory"] = server.transport
            super().__init__(logger, **kwargs)
            self._fake_started = False

        async def _open(self, tool: Any) -> Any:
            if not self._fake_started:
                self._fake_started = True
                await server.__aenter__()
            return await super()._open(tool)

        async def aclose(self) -> None:
            await super().aclose()
            if self._fake_started:
                await server.__aexit__(None, None, None)

    def create_model(self: Any, config: Any) -> FakeChatModel:
        return model

    with (
        startup_world(Events(), agents=()),
        patch.object(di.ProviderRegistry, "create_model", create_model),
        patch.object(di, "McpSessions", SessionsOnFakeServer),
    ):
        app = app_factory.AppFactory().create_app()
        with TestClient(app, base_url=LOOPBACK_BASE_URL, client=LOOPBACK_CLIENT, raise_server_exceptions=False) as http:
            smoke = _Smoke(http, server, model)
            smoke.methods_after_startup = list(server.methods())
            yield smoke
    smoke.open_after_shutdown = [r.method for r in server.requests]


@pytest.mark.usefixtures("offline_knowledge")
def test_sessao_abre_no_startup_so_a_tool_conferida_chega_ao_modelo_e_o_shutdown_fecha_a_sessao(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.DEBUG)
    script = [_tool_call("c1", "gh__search", {"q": "bug"}), "fim"]
    with _smoke_app(monkeypatch, tmp_path, script) as smoke:
        startup = list(smoke.methods_after_startup)
        run = smoke.http.post("/agents/ysol/runs", data={"message": "oi", "stream": "false"}, headers=RUN)
        calls = list(smoke.server.calls)
        call_auth = {
            r.headers.get("authorization")
            for r in smoke.server.requests
            if r.method == "POST" and json.loads(r.content).get("method") == "tools/call"
        }
        exposed = [call.tool_names for call in smoke.model.calls]
        tool_results = [str(content) for role, content in smoke.model.calls[-1].messages if role == "tool"]
        before_shutdown = len(smoke.server.requests)

    assert startup[:2] == ["initialize", "notifications/initialized"] and "tools/list" in startup
    assert run.status_code == 200, run.text
    assert run.json()["content"] == "fim"
    assert exposed == [("gh__search",), ("gh__search",)]  # nada de delete_repo, nem da tool de hash divergente
    assert calls == [("search", {"q": "bug"})]
    assert call_auth == {LEAK_MARKER}  # a referência foi resolvida e foi ao servidor declarado
    assert any("ok search" in content for content in tool_results)
    for visible in (run.text, "\n".join(tool_results), caplog.text):
        assert LEAK_MARKER not in visible
    # o hash divergente foi recusado com log (nunca o schema), e o app subiu
    assert "diverge do hash fixado" in caplog.text
    assert "Apaga o repositório" not in caplog.text
    # shutdown do lifespan: DELETE da sessão enviado, e nenhum request depois do fechamento
    methods = smoke.open_after_shutdown
    assert methods[before_shutdown:] == ["DELETE", "DELETE"]  # uma sessão por servidor (gh e outro), só DELETE
