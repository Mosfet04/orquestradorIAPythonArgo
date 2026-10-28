"""FA-05: ``GET /admin/config/{kind}/{id}/history`` e ``POST .../history/{version}/restore`` pela borda real.

Repositórios em memória, runtime falso e o diário do container sobre o Mongo falso (``admin_world``); o store
YAML (só leitura, sem diário) pela pilha completa (``yaml_app``).
"""

from __future__ import annotations

from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI

from src.presentation.routes.admin_config import router as admin_config_router
from tests.fakes.admin_config_app import ADMIN, admin_world, agent_body, fake_config_admin, if_match
from tests.fakes.logger import RecordingLogger
from tests.fakes.repositories import (
    InMemoryAgentConfigRepository,
    InMemoryTeamConfigRepository,
    InMemoryToolRepository,
)
from tests.fakes.yaml_world import ADMIN as YAML_ADMIN
from tests.fakes.yaml_world import yaml_app

BASE = "/admin/config"
YAML_CONFIG = """
agents:
  - {id: y1, nome: Agente do YAML, factoryIaModel: ollama, model: m-y1, prompt: p, active: true}
"""


async def test_historico_lista_as_escritas_do_id_da_mais_nova_com_ator_data_diff_e_documento(
    monkeypatch: pytest.MonkeyPatch,
):
    async with admin_world(monkeypatch) as world:
        await world.client.post(f"{BASE}/agents", json=agent_body("a1"), headers=ADMIN)
        await world.client.put(f"{BASE}/agents/a1", json=agent_body("a1", prompt="novo"), headers=if_match(1))
        await world.client.post(f"{BASE}/agents", json=agent_body("a2"), headers=ADMIN)
        history = await world.client.get(f"{BASE}/agents/a1/history", headers=ADMIN)
        page = await world.client.get(f"{BASE}/agents/a1/history?offset=1&limit=1", headers=ADMIN)
        empty = await world.client.get(f"{BASE}/teams/a1/history", headers=ADMIN)

    assert history.status_code == 200
    body = history.json()
    assert (body["total"], body["offset"], body["limit"]) == (2, 0, 50)
    latest, first = body["items"]
    assert (latest["version"], latest["actor"]) == (2, "admin")
    assert latest["diff"] == {"prompt": {"before": "p", "after": "novo"}}
    assert latest["document"]["prompt"] == "novo"
    assert latest["at"].endswith("+00:00")
    assert first["version"] == 1 and first["diff"]["id"] == {"before": None, "after": "a1"}
    assert [item["version"] for item in page.json()["items"]] == [1]
    assert empty.json() == {"items": [], "total": 0, "offset": 0, "limit": 50}


async def test_restaurar_responde_o_documento_com_etag_e_revisao_e_exige_if_match(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch) as world:
        await world.client.post(f"{BASE}/agents", json=agent_body("a1", nome="Um"), headers=ADMIN)
        await world.client.put(f"{BASE}/agents/a1", json=agent_body("a1", nome="Dois"), headers=if_match(1))
        missing = await world.client.post(f"{BASE}/agents/a1/history/1/restore", headers=ADMIN)
        stale = await world.client.post(f"{BASE}/agents/a1/history/1/restore", headers=if_match(1))
        restored = await world.client.post(f"{BASE}/agents/a1/history/1/restore", headers=if_match(2))
        unknown = [
            await world.client.post(f"{BASE}/agents/a1/history/{version}/restore", headers=if_match(3))
            for version in ("9", "x", "1" * 30)
        ]

    assert missing.status_code == 428
    assert stale.status_code == 412
    assert restored.status_code == 200
    assert (restored.json()["nome"], restored.json()["version"]) == ("Um", 3)
    assert (restored.headers["ETag"], restored.headers["Runtime-Revision"]) == ('"3"', "3")
    assert [(r.status_code, r.json()) for r in unknown] == [(404, {"detail": "Config não encontrada"})] * 3


@pytest.mark.usefixtures("offline_knowledge")
def test_store_yaml_nao_tem_historico_e_restaurar_responde_409(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    with yaml_app(monkeypatch, tmp_path, YAML_CONFIG, routers=[admin_config_router]) as (_, _path, http):
        history = http.get(f"{BASE}/agents/y1/history", headers=YAML_ADMIN)
        restored = http.post(f"{BASE}/agents/y1/history/1/restore", headers={**YAML_ADMIN, "If-Match": '"0"'})

    assert history.json() == {"items": [], "total": 0, "offset": 0, "limit": 50}
    assert (restored.status_code, restored.json()) == (409, {"detail": "O store de configuração é somente leitura"})


async def test_escrita_sem_o_id_da_chave_do_middleware_responde_401_sem_gravar():
    """Router montado sem o ``ApiKeyAuthMiddleware``: sem ator autenticado, nada é gravado nem auditado."""
    agents = InMemoryAgentConfigRepository()
    app = FastAPI()
    app.include_router(admin_config_router)
    app.state.config_admin = fake_config_admin(
        agents, InMemoryTeamConfigRepository(), InMemoryToolRepository(), RecordingLogger()
    )
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"{BASE}/agents", json=agent_body("a1"))

    assert (response.status_code, response.json()) == (401, {"detail": "unauthorized"})
    assert await agents.list_all() == []
