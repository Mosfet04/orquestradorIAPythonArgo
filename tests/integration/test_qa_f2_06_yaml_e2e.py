"""QA do F2-06: app real com ``CONFIG_STORE=yaml`` de ponta a ponta.

Pilha: ``AppFactory`` (lifespan) -> container -> repositórios YAML -> use cases -> ``AgnoRuntime`` ->
AgentOS, sobre ``tests/fakes/startup_world.py`` (Mongo, db de sessões e modelo falsos). Aqui o modelo
é um ``FakeChatModel`` roteirizado por ``model_id`` e a tool HTTP do YAML fala com um
``httpx.MockTransport`` (nada sai da máquina). Chaves são valores de teste.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from agno.models.response import ModelResponse
from starlette.testclient import TestClient

from src.infrastructure import dependency_injection as di
from src.infrastructure.repositories.yaml_config_repository import YamlConfigError
from src.infrastructure.web import app_factory
from tests.fakes import FakeChatModel, RecordingLogger
from tests.fakes.agui import assert_valid_run, parse_agui_sse, text_of, types_of
from tests.fakes.startup_world import CLEAN_ENV, Events, startup_world
from tests.fakes.web import LOOPBACK_BASE_URL, LOOPBACK_CLIENT

RUN_KEY = "qa6-run-key-" + "r" * 21
ADMIN_KEY = "qa6-admin-key-" + "a" * 19
RUN = {"Authorization": f"Bearer {RUN_KEY}"}
ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}
BASE = "https://api.example.invalid"
MARKER = "SEGREDO-DO-YAML-QA"

CONFIG = f"""
agents:
  - texto solto, não é documento
  - id: ysol
    nome: Agente do YAML
    factoryIaModel: ollama
    model: m-ysol
    descricao: d
    prompt: [Você é um agente de teste.]
    tools_ids: [cep]
    active: true
  - {{id: quebrado, nome: Q, factoryIaModel: ollama, model: m, prompt: "{MARKER}", active: true, rag_config: abc}}
  - {{id: inativo, nome: I, factoryIaModel: ollama, model: m-inativo, prompt: p, active: false}}
tools:
  - id: cep
    name: CEP
    description: Busca endereço pelo CEP
    route: {BASE}/cep/{{cep}}
    http_method: GET
    parameters:
      - {{name: cep, type: string, description: CEP, required: true}}
    active: true
  - {{id: cep, name: CEP2, description: repetida, route: "{BASE}/outra", active: true}}
"""


def _tool_call(call_id: str = "c1") -> ModelResponse:
    arguments = json.dumps({"cep": "01001000"})
    return ModelResponse(
        role="assistant",
        tool_calls=[{"id": call_id, "type": "function", "function": {"name": "cep", "arguments": arguments}}],
    )


class _World:
    def __init__(self) -> None:
        self.models: dict[str, FakeChatModel] = {}
        self.requests: list[httpx.Request] = []
        self.logger = RecordingLogger()


@contextmanager
def yaml_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    text: str,
    *,
    scripts: dict[str, list[str | ModelResponse]] | None = None,
) -> Iterator[tuple[_World, Path, TestClient]]:
    for name in CLEAN_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    path = tmp_path / "config.yaml"
    path.write_text(text, encoding="utf-8")
    for name, value in {
        "CONFIG_STORE": "yaml",
        "CONFIG_YAML_PATH": str(path),
        "ENVIRONMENT": "production",
        "APP_HOST": "127.0.0.1",
        "API_KEY_RUN": RUN_KEY,
        "API_KEY_ADMIN": ADMIN_KEY,
    }.items():
        monkeypatch.setenv(name, value)

    world = _World()
    models = {key: FakeChatModel(id=key, responses=list(script)) for key, script in (scripts or {}).items()}
    world.models = models

    def create_model(self: Any, config: Any) -> FakeChatModel:
        return models.setdefault(config.model_id, FakeChatModel(id=config.model_id, responses=["x"] * 20))

    def transport_handler(request: httpx.Request) -> httpx.Response:
        world.requests.append(request)
        return httpx.Response(200, json={"logradouro": "Praça da Sé"})

    real_client: Callable[..., httpx.AsyncClient] = httpx.AsyncClient

    def mocked_client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(transport_handler), **kwargs)

    with (
        startup_world(Events(), agents=("so-no-mongo",)),
        patch.object(di.ProviderRegistry, "create_model", create_model),
        patch.object(httpx, "AsyncClient", mocked_client),
    ):
        app = app_factory.AppFactory().create_app()
        with TestClient(app, base_url=LOOPBACK_BASE_URL, client=LOOPBACK_CLIENT, raise_server_exceptions=False) as http:
            yield world, path, http


@pytest.mark.usefixtures("offline_knowledge")
def test_run_rest_e_agui_de_agente_do_yaml_executam_a_tool_http_do_yaml(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    scripts: dict[str, list[str | ModelResponse]] = {
        "m-ysol": [_tool_call("c1"), "resposta REST", _tool_call("c2"), "resposta AGUI"]
    }
    with yaml_app(monkeypatch, tmp_path, CONFIG, scripts=scripts) as (world, _, http):
        listed = http.get("/agents", headers=RUN)
        rest = http.post("/agents/ysol/runs", data={"message": "oi", "stream": "false"}, headers=RUN)
        agui = http.post(
            "/agui/ysol",
            json={
                "threadId": "t1", "runId": "r1", "state": {}, "tools": [], "context": [], "forwardedProps": {},
                "messages": [{"id": "m1", "role": "user", "content": "oi"}],
            },
            headers=RUN,
        )
        no_key = http.post("/agents/ysol/runs", data={"message": "oi", "stream": "false"})
        missing = http.post("/agents/so-no-mongo/runs", data={"message": "oi", "stream": "false"}, headers=RUN)

    # só o agente válido e ativo do YAML (o do "Mongo", o inativo, o quebrado e o texto solto ficam de fora)
    assert [a["id"] for a in listed.json()] == ["ysol"]
    assert rest.status_code == 200, rest.text
    assert rest.json()["content"] == "resposta REST"
    events = parse_agui_sse(agui.text)
    assert_valid_run(events)
    assert text_of(events) == "resposta AGUI"
    assert {"TOOL_CALL_START", "TOOL_CALL_RESULT"} <= set(types_of(events))
    # a tool veio do YAML (a primeira com o id; a repetida, com outra rota, nunca foi chamada)
    assert [(r.method, str(r.url)) for r in world.requests] == [("GET", f"{BASE}/cep/01001000")] * 2
    assert any(
        role == "tool" and "Praça da Sé" in content for role, content in world.models["m-ysol"].calls[1].messages
    )
    assert no_key.status_code == 401
    assert missing.status_code == 404
    assert MARKER not in rest.text + agui.text + listed.text


@pytest.mark.usefixtures("offline_knowledge")
def test_refresh_apos_editar_o_yaml_atualiza_o_cache_mas_nao_as_rotas(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Coerente com o README: o refresh relê o arquivo (cache interno); rotas só mudam reiniciando."""
    with yaml_app(monkeypatch, tmp_path, CONFIG) as (_, path, http):
        before = http.get("/metrics/cache", headers=ADMIN).json()
        new_agent = "  - {id: novo, nome: N, factoryIaModel: ollama, model: m-novo, prompt: p, active: true}\n"
        path.write_text(CONFIG.replace("tools:\n", new_agent + "tools:\n"), encoding="utf-8")
        refresh = http.post("/admin/refresh-cache", headers=ADMIN)
        after = http.get("/metrics/cache", headers=ADMIN).json()
        served = http.get("/agents", headers=RUN).json()
        run_new = http.post("/agents/novo/runs", data={"message": "oi", "stream": "false"}, headers=RUN)

    assert before["agents"]["agent_count"] == 1
    assert refresh.status_code == 200 and refresh.json() == {"status": "cache_refreshed"}
    assert after["agents"]["agent_count"] == 2
    assert [a["id"] for a in served] == ["ysol"]
    assert run_new.status_code == 404


def _break_then_refresh(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, Any]:
    with yaml_app(monkeypatch, tmp_path, CONFIG, scripts={"m-ysol": ["ainda funciona"]}) as (_, path, http):
        path.write_text(f"agentes:\n  - {MARKER}\n", encoding="utf-8")
        refresh = http.post("/admin/refresh-cache", headers=ADMIN)
        cache = http.get("/metrics/cache", headers=ADMIN)
        served = http.get("/agents", headers=RUN)
        run = http.post("/agents/ysol/runs", data={"message": "oi", "stream": "false"}, headers=RUN)
    return {"refresh": refresh, "cache": cache, "served": served, "run": run}


@pytest.mark.usefixtures("offline_knowledge")
def test_refresh_com_yaml_quebrado_depois_do_startup_erra_sem_conteudo_e_o_agentos_segue_servindo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    out = _break_then_refresh(monkeypatch, tmp_path)

    assert out["refresh"].status_code == 500
    assert all(MARKER not in r.text for r in out.values())
    assert out["served"].status_code == 200 and [a["id"] for a in out["served"].json()] == ["ysol"]
    assert out["run"].status_code == 200 and out["run"].json()["content"] == "ainda funciona"


@pytest.mark.usefixtures("offline_knowledge")
def test_refresh_com_yaml_quebrado_mantem_o_cache_anterior_como_diz_o_readme(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    out = _break_then_refresh(monkeypatch, tmp_path)

    assert out["cache"].json()["agents"]["status"] == "active", out["cache"].json()
    assert out["cache"].json()["agents"]["agent_count"] == 1


HOSTILE = {
    "tag-python": f"agents: !!python/object/apply:os.system ['echo {MARKER}']\n",
    "tag-objeto": f"agents:\n  - !!python/object:os.environ {{x: {MARKER}}}\n",
    "yaml-quebrado": f"agents: [{{id: {MARKER}\n",
    "topo-lista": f"- {MARKER}\n",
    "chave-desconhecida": f"agentes:\n  - id: {MARKER}\n",
    "secao-mapa": f"agents:\n  id: {MARKER}\n",
    "varios-documentos": f"agents: []\n---\nteams: [{MARKER}]\n",
}


@pytest.mark.usefixtures("offline_knowledge")
@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_yaml_hostil_no_startup_recusa_subir_sem_o_conteudo_no_erro(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, name: str
):
    with pytest.raises(YamlConfigError) as exc_info, yaml_app(monkeypatch, tmp_path, HOSTILE[name]):
        pass

    assert MARKER not in str(exc_info.value)
    assert exc_info.value.__cause__ is None


@pytest.mark.usefixtures("offline_knowledge")
@pytest.mark.parametrize(
    "text",
    ["", "# só comentário\n", "agents:\n", "agents: []\nteams: []\ntools: []\n", "﻿agents: []\n"],
    ids=["vazio", "so-comentario", "secao-nula", "listas-vazias", "bom"],
)
def test_yaml_sem_nenhuma_entidade_sobe_sem_agentos_e_serve_saude(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, text: str
):
    with yaml_app(monkeypatch, tmp_path, text) as (_, _, http):
        agents = http.get("/agents", headers=RUN)
        health = http.get("/admin/health", headers=ADMIN)
        cache = http.get("/metrics/cache", headers=ADMIN).json()

    # igual a coleções vazias no Mongo (F1-10): o AgentOS não é montado, só os endpoints admin
    assert agents.status_code == 404
    assert health.status_code == 200
    assert cache["agents"]["agent_count"] == 0


@pytest.mark.usefixtures("offline_knowledge")
def test_yaml_com_todos_os_documentos_invalidos_sobe_sem_entidades(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    text = f"agents:\n  - {MARKER}\n  - {{id: 5, nome: x, model: m, prompt: p, active: true}}\n  - [a]\n"
    with yaml_app(monkeypatch, tmp_path, text) as (_, _, http):
        agents = http.get("/agents", headers=RUN)
        cache = http.get("/metrics/cache", headers=ADMIN).json()

    assert agents.status_code == 404
    assert cache["agents"]["agent_count"] == 0


@pytest.mark.usefixtures("offline_knowledge")
@pytest.mark.parametrize(
    ("store", "path_kind", "message"),
    [
        ("redis", "ok", "CONFIG_STORE inválido"),
        ("yaml", "ausente-na-env", "CONFIG_YAML_PATH é obrigatória"),
        ("yaml", "inexistente", "arquivo regular legível"),
        ("yaml", "diretorio", "arquivo regular legível"),
    ],
)
def test_app_recusa_subir_com_config_store_ou_caminho_invalidos_sem_ecoar_o_valor(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, store: str, path_kind: str, message: str
):
    for name in CLEAN_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.delenv("CONFIG_YAML_PATH", raising=False)
    (tmp_path / "dir").mkdir()
    (tmp_path / "ok.yaml").write_text("agents: []\n", encoding="utf-8")
    paths = {"inexistente": tmp_path / MARKER, "diretorio": tmp_path / "dir", "ok": tmp_path / "ok.yaml"}
    monkeypatch.setenv("CONFIG_STORE", store)
    if path_kind in paths:
        monkeypatch.setenv("CONFIG_YAML_PATH", str(paths[path_kind]))

    with startup_world(Events()), pytest.raises(ValueError, match=message) as exc_info:
        with TestClient(app_factory.AppFactory().create_app()):
            pass

    assert MARKER not in str(exc_info.value)
