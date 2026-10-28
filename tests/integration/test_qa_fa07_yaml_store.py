"""QA do FA-07: store YAML (só leitura) com os campos de execução, na pilha real (lifespan -> AgentOS).

O YAML declara ``markdown``, ``num_history_runs``... só em snake_case. Os valores chegam ao agente servido
e à API admin; texto onde se espera booleano (``markdown: "false"``) invalida só aquele documento.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.presentation.routes.admin_config import router as admin_config_router
from tests.fakes.yaml_world import ADMIN, RUN, yaml_app

CONFIG = """
agents:
  - id: sem
    nome: Sem campos
    factoryIaModel: ollama
    model: m-sem
    descricao: d
    prompt: [Instrução do agente sem campos.]
    active: true
  - id: com
    nome: Com campos
    factory_ia_model: ollama
    model: m-com
    descricao: d
    prompt: [Instrução do agente com campos.]
    markdown: false
    add_datetime_to_context: false
    num_history_runs: 1
    active: true
  - id: ruim
    nome: Texto no booleano
    factoryIaModel: ollama
    model: m-ruim
    descricao: d
    prompt: [x]
    markdown: "false"
    active: true
"""
FIELDS = ("markdown", "add_history_to_context", "num_history_runs", "add_datetime_to_context", "reasoning")


def _system(world, model_id: str) -> str:
    messages = world.models[model_id].calls[-1].messages
    return next(content for role, content in messages if role == "system")


@pytest.mark.usefixtures("offline_knowledge")
def test_yaml_aplica_os_campos_ao_agente_servido_e_a_api_os_mostra(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    with yaml_app(monkeypatch, tmp_path, CONFIG, routers=[admin_config_router]) as (world, _, http):
        runs = {
            agent_id: http.post(f"/agents/{agent_id}/runs", data={"message": "oi", "stream": "false"}, headers=RUN)
            for agent_id in ("sem", "com")
        }
        sem = http.get("/admin/config/agents/sem", headers=ADMIN)
        com = http.get("/admin/config/agents/com", headers=ADMIN)

    assert {k: r.status_code for k, r in runs.items()} == {"sem": 200, "com": 200}
    assert "Use markdown to format your answers." in _system(world, "m-sem")
    assert "The current time is" in _system(world, "m-sem")
    assert "Use markdown" not in _system(world, "m-com") and "current time" not in _system(world, "m-com")
    assert {k: sem.json()[k] for k in FIELDS} == {
        "markdown": True,
        "add_history_to_context": True,
        "num_history_runs": 5,
        "add_datetime_to_context": True,
        "reasoning": False,
    }
    assert {k: com.json()[k] for k in FIELDS} == {
        "markdown": False,
        "add_history_to_context": True,
        "num_history_runs": 1,
        "add_datetime_to_context": False,
        "reasoning": False,
    }


@pytest.mark.usefixtures("offline_knowledge")
def test_yaml_com_texto_no_booleano_isola_so_o_documento(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    with yaml_app(monkeypatch, tmp_path, CONFIG, routers=[admin_config_router]) as (_, _, http):
        listed = http.get("/admin/config/agents", headers=ADMIN)
        served = http.post("/agents/ruim/runs", data={"message": "oi", "stream": "false"}, headers=RUN)
        healthy = http.post("/agents/sem/runs", data={"message": "oi", "stream": "false"}, headers=RUN)

    assert sorted(i["id"] for i in listed.json()["items"]) == ["com", "sem"]
    assert served.status_code == 404
    assert healthy.status_code == 200
