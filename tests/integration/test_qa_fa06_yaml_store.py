"""QA do FA-06: store YAML (só leitura) e o header por referência de ponta a ponta no app real.

Pilha de ``tests/fakes/yaml_world.py`` (lifespan -> container -> YAML -> ``AgnoRuntime`` -> AgentOS, modelo
roteirizado, upstream ``httpx.MockTransport``). Valores são de teste.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from agno.models.response import ModelResponse

from src.presentation.routes.admin_config import router as admin_config_router
from tests.fakes import yaml_world
from tests.fakes.admin_config_app import tool_body
from tests.fakes.yaml_world import ADMIN, BASE, RUN, yaml_app

SECRET_ENV = "valor-de-teste-do-ambiente"  # noqa: S105 - marcador de vazamento, não é segredo
SECRET_FILE = "valor-de-teste-do-arquivo"  # noqa: S105 - idem

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
tools:
  - id: cep
    name: CEP
    description: Busca endereço pelo CEP
    route: {BASE}/cep/{{cep}}
    http_method: GET
    parameters:
      - {{name: cep, type: string, description: CEP, required: true}}
    headers:
      Authorization: env:CEP_API_KEY
      X-Arquivo: file:{{secrets}}/cep
      X-Texto: legado-em-texto
    active: true
"""


def _tool_call(call_id: str) -> ModelResponse:
    arguments = json.dumps({"cep": "01001000"})
    return ModelResponse(
        role="assistant",
        tool_calls=[{"id": call_id, "type": "function", "function": {"name": "cep", "arguments": arguments}}],
    )


@pytest.mark.usefixtures("offline_knowledge")
def test_tool_do_yaml_resolve_referencias_na_chamada_e_o_segredo_nao_aparece_em_resposta_nem_listagem(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    secrets = tmp_path / "secrets"
    secrets.mkdir()
    (secrets / "cep").write_text(SECRET_FILE + "\n")
    # ``yaml_app`` limpa o ambiente de config (inclusive SECRETS_DIR): este teste precisa dele
    monkeypatch.setattr(yaml_world, "CLEAN_ENV", tuple(n for n in yaml_world.CLEAN_ENV if n != "SECRETS_DIR"))
    monkeypatch.setenv("SECRETS_DIR", str(secrets))
    monkeypatch.setenv("CEP_API_KEY", SECRET_ENV)
    scripts: dict[str, list[str | ModelResponse]] = {"m-ysol": [_tool_call("c1"), "ok 1", _tool_call("c2"), "ok 2"]}

    with yaml_app(
        monkeypatch, tmp_path, CONFIG.replace("{secrets}", str(secrets)), scripts=scripts, routers=[admin_config_router]
    ) as (world, _, http):
        first = http.post("/agents/ysol/runs", data={"message": "oi", "stream": "false"}, headers=RUN)
        monkeypatch.setenv("CEP_API_KEY", "valor-de-teste-rotacionado")
        (secrets / "cep").write_text("valor-de-teste-arquivo-novo")
        second = http.post("/agents/ysol/runs", data={"message": "oi", "stream": "false"}, headers=RUN)
        shown = http.get("/admin/config/tools/cep", headers=ADMIN)

    assert (first.status_code, second.status_code) == (200, 200)
    sent = [(r.headers["authorization"], r.headers["x-arquivo"], r.headers["x-texto"]) for r in world.requests]
    assert sent == [
        (SECRET_ENV, SECRET_FILE, "legado-em-texto"),
        ("valor-de-teste-rotacionado", "valor-de-teste-arquivo-novo", "legado-em-texto"),
    ]
    assert shown.json()["headers"] == {
        "Authorization": "env:CEP_API_KEY",
        "X-Arquivo": f"file:{secrets}/cep",
        "X-Texto": "***",  # o texto legado nunca sai
    }
    leaked = [s for s in (SECRET_ENV, SECRET_FILE, "legado-em-texto") if s in first.text + second.text + shown.text]
    assert leaked == []


@pytest.mark.usefixtures("offline_knowledge")
def test_escrita_de_tool_com_store_yaml_responde_409_antes_de_qualquer_guarda(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    text = CONFIG.replace("{secrets}", "/run/secrets")
    with yaml_app(monkeypatch, tmp_path, text, routers=[admin_config_router]) as (_, path, http):
        before = path.read_text(encoding="utf-8")
        metadata = http.post(
            "/admin/config/tools", json=tool_body(id="nova", route="http://169.254.169.254/x"), headers=ADMIN
        )
        sensitive = http.post(
            "/admin/config/tools", json=tool_body(id="nova", headers={"Authorization": "texto"}), headers=ADMIN
        )
        put = http.put(
            "/admin/config/tools/cep",
            json=tool_body(route="http://10.0.0.5/x"),
            headers={**ADMIN, "If-Match": '"0"'},
        )
        after = path.read_text(encoding="utf-8")

    assert (metadata.status_code, sensitive.status_code, put.status_code) == (409, 409, 409)
    assert before == after  # o arquivo do operador não é tocado
