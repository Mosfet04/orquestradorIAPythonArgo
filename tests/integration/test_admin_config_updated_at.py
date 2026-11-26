"""FA-08: ``updated_at`` (ISO 8601 UTC com ``Z``) nas respostas de ``/admin/config/{agents,teams,tools}``.

Pela borda real (auth + CORS), com os repositórios em memória e com os repositórios Mongo reais sobre a
coleção fake: lista, GET, POST, PUT, DELETE lógico e restauração mostram a hora da última gravação;
documento sem o campo (semeado, legado, YAML) ou com valor que não é datetime mostra ``null``. O
``updated_at`` do corpo de escrita é descartado, como o ``version``, e o OpenAPI documenta o campo.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI

from src.infrastructure.repositories.config_documents import entity_document
from src.presentation.routes.admin_config import router as admin_config_router
from src.presentation.routes.admin_config import updated_at_text
from tests.fakes.admin_config_app import (
    ADMIN,
    admin_world,
    agent,
    agent_body,
    fake_config_admin,
    if_match,
    team,
    tool,
)
from tests.fakes.config_stores import MongoBackend, agent_document
from tests.fakes.logger import RecordingLogger
from tests.fakes.yaml_world import ADMIN as YAML_ADMIN
from tests.fakes.yaml_world import yaml_app

BASE = "/admin/config"
ISO_UTC = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")
SENT = "1999-01-01T00:00:00Z"
"""``updated_at`` que o cliente manda no corpo: nunca pode ser gravado nem voltar na resposta."""


def _without_updated_at(body: dict[str, Any]) -> dict[str, Any]:
    return {key: value for key, value in body.items() if key != "updated_at"}


async def _stored_text(repo: Any, entity_id: str) -> str | None:
    found = next(item for item in await repo.list_all() if item.entity.id == entity_id)
    return updated_at_text(found.updated_at)


# ── repositórios em memória ──────────────────────────────────────────────────────────────────


async def test_documento_semeado_sem_o_campo_mostra_null_na_lista_e_no_get(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch, agents=[agent("a1")], teams=[team("t1", ["a1"])], tools=[tool("cep")]) as world:
        listed = {k: await world.client.get(f"{BASE}/{k}", headers=ADMIN) for k in ("agents", "teams", "tools")}
        fetched = await world.client.get(f"{BASE}/agents/a1", headers=ADMIN)

    for response in listed.values():
        [item] = response.json()["items"]
        assert "updated_at" in item and item["updated_at"] is None
    assert fetched.json()["updated_at"] is None and fetched.json()["version"] == 0


async def test_post_mostra_a_hora_da_gravacao_e_a_lista_e_o_get_mostram_a_mesma(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch) as world:
        before = updated_at_text(datetime.now(UTC))
        created = await world.client.post(f"{BASE}/agents", json=agent_body("a1"), headers=ADMIN)
        after = updated_at_text(datetime.now(UTC))
        listed = await world.client.get(f"{BASE}/agents", headers=ADMIN)
        fetched = await world.client.get(f"{BASE}/agents/a1", headers=ADMIN)
        stored = await _stored_text(world.agents, "a1")

    assert created.status_code == 201
    shown = created.json()["updated_at"]
    assert ISO_UTC.match(shown) and before <= shown <= after
    assert shown == stored
    assert listed.json()["items"][0]["updated_at"] == shown
    assert fetched.json()["updated_at"] == shown
    # nenhum outro campo muda: o corpo é o documento da entidade mais version e updated_at
    assert set(created.json()) - set(entity_document(agent("a1"))) == {"version", "updated_at"}
    assert _without_updated_at(created.json()) == _without_updated_at(fetched.json())


async def test_put_delete_logico_e_restauracao_mostram_a_hora_da_propria_gravacao(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch, agents=[agent("a1")]) as world:
        replaced = await world.client.put(f"{BASE}/agents/a1", json=agent_body("a1", prompt="v1"), headers=if_match(0))
        after_put = await _stored_text(world.agents, "a1")
        deleted = await world.client.delete(f"{BASE}/agents/a1", headers=if_match(1))
        after_delete = await _stored_text(world.agents, "a1")
        restored = await world.client.post(f"{BASE}/agents/a1/history/1/restore", headers=if_match(2))
        after_restore = await _stored_text(world.agents, "a1")
        fetched = await world.client.get(f"{BASE}/agents/a1", headers=ADMIN)
        history = await world.client.get(f"{BASE}/agents/a1/history", headers=ADMIN)

    assert (replaced.status_code, deleted.status_code, restored.status_code) == (200, 200, 200)
    assert replaced.json()["updated_at"] == after_put and ISO_UTC.match(after_put)
    assert deleted.json()["updated_at"] == after_delete and deleted.json()["active"] is False
    assert restored.json()["updated_at"] == after_restore and restored.json()["version"] == 3
    assert fetched.json()["updated_at"] == after_restore
    assert after_put <= after_delete <= after_restore
    # o histórico não ganha o campo: o documento dele é o da entidade, como antes
    assert all("updated_at" not in entry["document"] for entry in history.json()["items"])


async def test_updated_at_do_corpo_e_descartado_no_post_e_no_put(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch) as world:
        created = await world.client.post(f"{BASE}/agents", json=agent_body("a1", updated_at=SENT), headers=ADMIN)
        fetched = await world.client.get(f"{BASE}/agents/a1", headers=ADMIN)
        # ciclo GET -> PUT da UI: o corpo volta com version e updated_at
        replaced = await world.client.put(
            f"{BASE}/agents/a1", json={**fetched.json(), "updated_at": {"$set": 1}}, headers=if_match(1)
        )

    assert (created.status_code, replaced.status_code) == (201, 200)
    assert created.json()["updated_at"] != SENT and replaced.json()["updated_at"] != SENT
    stored = (await world.agents.list_all())[0]
    assert not hasattr(stored.entity, "updated_at")
    assert updated_at_text(stored.updated_at) == replaced.json()["updated_at"]


# ── repositórios Mongo reais sobre a coleção fake ───────────────────────────────────────────


def _mongo_repos(monkeypatch: pytest.MonkeyPatch, agents: list[dict[str, Any]]):
    backend = MongoBackend(monkeypatch)
    logger = RecordingLogger()
    return backend, backend.agents(agents, logger), backend.teams([], logger), backend.tools([], logger), logger


async def test_mongo_legado_e_valor_que_nao_e_datetime_mostram_null_e_naive_sai_em_utc(
    monkeypatch: pytest.MonkeyPatch,
):
    documents = [
        agent_document(agent("legado")),
        {**agent_document(agent("texto")), "updated_at": "2026-11-26 14:03"},
        {**agent_document(agent("naive")), "updated_at": datetime(2026, 11, 26, 14, 3, 0, 250_000), "version": 4},
    ]
    _, agents, teams, tools, logger = _mongo_repos(monkeypatch, documents)
    async with admin_world(monkeypatch) as world:
        world.app.state.config_admin = fake_config_admin(agents, teams, tools, logger)
        listed = await world.client.get(f"{BASE}/agents", headers=ADMIN)
        fetched = await world.client.get(f"{BASE}/agents/naive", headers=ADMIN)

    shown = {item["id"]: item["updated_at"] for item in listed.json()["items"]}
    assert shown == {"legado": None, "texto": None, "naive": "2026-11-26T14:03:00Z"}
    assert fetched.json()["updated_at"] == "2026-11-26T14:03:00Z" and fetched.json()["version"] == 4
    assert logger.messages("warning", "error") == []


async def test_mongo_escrita_mostra_a_hora_gravada_no_documento_e_descarta_a_do_corpo(
    monkeypatch: pytest.MonkeyPatch,
):
    backend, agents, teams, tools, logger = _mongo_repos(monkeypatch, [])
    async with admin_world(monkeypatch) as world:
        world.app.state.config_admin = fake_config_admin(agents, teams, tools, logger)
        created = await world.client.post(f"{BASE}/agents", json=agent_body("a1", updated_at=SENT), headers=ADMIN)
        at_create = backend.collections["agents_config"].docs[0]["updated_at"]
        fetched = await world.client.get(f"{BASE}/agents/a1", headers=ADMIN)
        replaced = await world.client.put(f"{BASE}/agents/a1", json=fetched.json(), headers=if_match(1))
        at_replace = backend.collections["agents_config"].docs[0]["updated_at"]
        deleted = await world.client.delete(f"{BASE}/agents/a1", headers=if_match(2))
        at_delete = backend.collections["agents_config"].docs[0]["updated_at"]
        restored = await world.client.post(f"{BASE}/agents/a1/history/2/restore", headers=if_match(3))
        at_restore = backend.collections["agents_config"].docs[0]["updated_at"]
        listed = await world.client.get(f"{BASE}/agents", headers=ADMIN)

    assert [r.status_code for r in (created, fetched, replaced, deleted, restored)] == [201, 200, 200, 200, 200]
    assert all(isinstance(moment, datetime) for moment in (at_create, at_replace, at_delete, at_restore))
    assert created.json()["updated_at"] == updated_at_text(at_create) == fetched.json()["updated_at"]
    assert replaced.json()["updated_at"] == updated_at_text(at_replace)
    assert deleted.json()["updated_at"] == updated_at_text(at_delete)
    assert restored.json()["updated_at"] == updated_at_text(at_restore)
    assert listed.json()["items"][0]["updated_at"] == updated_at_text(at_restore)
    assert SENT not in {r.json()["updated_at"] for r in (created, replaced, deleted, restored)}


# ── store YAML ───────────────────────────────────────────────────────────────────────────


YAML_CONFIG = """
agents:
  - {id: y1, nome: Agente do YAML, factoryIaModel: ollama, model: m-y1, prompt: p, active: true,
     updated_at: 2026-11-26T14:03:00Z}
"""


@pytest.mark.usefixtures("offline_knowledge")
def test_store_yaml_mostra_null_mesmo_com_a_chave_no_arquivo(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    with yaml_app(monkeypatch, tmp_path, YAML_CONFIG, routers=[admin_config_router]) as (_world, _path, http):
        listed = http.get(f"{BASE}/agents", headers=YAML_ADMIN)
        fetched = http.get(f"{BASE}/agents/y1", headers=YAML_ADMIN)

    assert [(item["id"], item["updated_at"]) for item in listed.json()["items"]] == [("y1", None)]
    assert fetched.json()["updated_at"] is None


# ── OpenAPI ──────────────────────────────────────────────────────────────────────────────


def _openapi() -> dict[str, Any]:
    app = FastAPI()
    app.include_router(admin_config_router)
    return app.openapi()


def _json_schema(operation: dict[str, Any], status: str) -> dict[str, Any]:
    return operation["responses"][status]["content"]["application/json"]["schema"]


@pytest.mark.parametrize("kind", ["agents", "teams", "tools"])
def test_openapi_documenta_updated_at_nas_respostas_do_documento(kind: str):
    paths = _openapi()["paths"]
    item = f"/admin/config/{kind}/{{entity_id}}"
    documents = {
        "list": _json_schema(paths[f"/admin/config/{kind}"]["get"], "200")["properties"]["items"]["items"],
        "get": _json_schema(paths[item]["get"], "200"),
        "post": _json_schema(paths[f"/admin/config/{kind}"]["post"], "201"),
        "put": _json_schema(paths[item]["put"], "200"),
        "delete": _json_schema(paths[item]["delete"], "200"),
        "restore": _json_schema(paths[f"{item}/history/{{version}}/restore"]["post"], "200"),
    }

    for operation, schema in documents.items():
        updated_at = schema["properties"]["updated_at"]
        assert updated_at["anyOf"] == [{"type": "string", "format": "date-time"}, {"type": "null"}], operation
        assert schema["properties"]["version"]["type"] == "integer", operation
        assert {"version", "updated_at"} <= set(schema["required"]), operation
        assert schema["additionalProperties"] is True, operation
