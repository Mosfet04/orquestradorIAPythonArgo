"""FA-03: ``GET /admin/schema`` pela borda real (auth + CORS) com o schema montado como o container monta.

O plugin de provider permitido pela ``PLUGIN_ALLOWLIST`` entra no schema pelo registry do app, sem mudar a
rota; o store gravável (Mongo) ou só leitura (YAML) vem do composition root, na pilha completa.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
from starlette.testclient import TestClient

from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.dependency_injection import build_provider_registry
from src.infrastructure.web import app_factory
from src.presentation.routes.admin_config import router as admin_config_router
from src.presentation.routes.admin_schema import router as admin_schema_router
from tests.fakes import RecordingLogger
from tests.fakes.admin_config_app import ADMIN, admin_env, admin_world, agent_body
from tests.fakes.plugins import FakeSite, spec_module
from tests.fakes.startup_world import Events, startup_world
from tests.fakes.web import LOOPBACK_BASE_URL, LOOPBACK_CLIENT
from tests.fakes.yaml_world import ADMIN as YAML_ADMIN
from tests.fakes.yaml_world import yaml_app

SCHEMA = "/admin/schema"
ROUTERS = [admin_config_router, admin_schema_router]


def _names(schema: dict[str, Any], kind: str) -> list[str]:
    return [option["name"] for option in schema["providers"][kind]]


async def test_plugin_permitido_pela_allowlist_aparece_no_schema_e_o_de_fora_nao(
    plugin_site: FakeSite, monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_site.install(
        "acme-plugin",
        {"acme": "acme_schema_mod:SPEC", "extra": "extra_schema_mod:SPEC"},
        {"acme_schema_mod": spec_module("acme", "acme-ai"), "extra_schema_mod": spec_module("extra")},
    )
    admin_env(monkeypatch)
    monkeypatch.setenv("PLUGIN_ALLOWLIST", "acme-plugin:acme")
    registry = build_provider_registry(AppConfig.load(), RecordingLogger())

    async with admin_world(monkeypatch, providers=registry) as world:
        response = await world.client.get(SCHEMA, headers=ADMIN)

    assert response.status_code == 200
    schema = response.json()
    chat = {option["name"]: option for option in schema["providers"]["chat"]}
    assert chat["acme"] == {"name": "acme", "model_params": ["temperature"], "requires_base_url": False}
    assert {"acme", "acme-ai"} <= set(_names(schema, "embedder"))
    assert "extra" not in chat
    assert {"ollama", "openai", "anthropic"} <= set(chat)


async def test_schema_descreve_enumeracoes_segredo_por_referencia_e_store(monkeypatch: pytest.MonkeyPatch) -> None:
    async with admin_world(monkeypatch) as world:
        response = await world.client.get(SCHEMA, headers=ADMIN)

    schema = response.json()
    assert set(schema) == {"providers", "agent", "team", "tool", "rag_config", "api_key_ref", "config_store"}
    assert schema["team"]["mode"] == ["route", "coordinate", "broadcast", "tasks"]
    assert schema["agent"]["num_history_runs"] == {"type": "integer", "default": 5, "minimum": 1, "maximum": 50}
    assert schema["team"]["markdown"] == {"type": "boolean", "default": True}
    assert schema["tool"]["http_method"] == ["GET", "POST", "PUT", "DELETE", "PATCH"]
    assert schema["rag_config"]["search_strategy"] == ["semantic", "hierarchical"]
    assert schema["api_key_ref"] == {"formats": ["env:<PROVEDOR>_API_KEY", "file:<caminho em SECRETS_DIR>"]}
    assert schema["config_store"] == {"writable": True}


async def test_sem_o_schema_no_estado_do_app_responde_503(monkeypatch: pytest.MonkeyPatch) -> None:
    async with admin_world(monkeypatch) as world:
        del world.app.state.config_schema
        response = await world.client.get(SCHEMA, headers=ADMIN)

    assert response.status_code == 503
    assert response.json() == {"detail": "Schema da configuração indisponível"}


@contextmanager
def _mongo_app(monkeypatch: pytest.MonkeyPatch) -> Iterator[TestClient]:
    admin_env(monkeypatch)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    with startup_world(Events(), agents=()):
        app = app_factory.AppFactory().create_app(routers=ROUTERS)
        with TestClient(app, base_url=LOOPBACK_BASE_URL, client=LOOPBACK_CLIENT) as http:
            yield http


def test_pilha_completa_com_store_mongo_responde_gravavel(monkeypatch: pytest.MonkeyPatch) -> None:
    with _mongo_app(monkeypatch) as http:
        response = http.get(SCHEMA, headers=ADMIN)
        written = http.post("/admin/config/agents", json=agent_body("novo"), headers=ADMIN)

    assert response.status_code == 200
    assert response.json()["config_store"] == {"writable": True}
    assert written.status_code == 201


@pytest.mark.usefixtures("offline_knowledge")
def test_pilha_completa_com_store_yaml_responde_somente_leitura(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with yaml_app(monkeypatch, tmp_path, "agents: []\n", routers=ROUTERS) as (_, path, http):
        before = path.read_bytes()
        response = http.get(SCHEMA, headers=YAML_ADMIN)
        written = http.post("/admin/config/agents", json=agent_body("novo"), headers=YAML_ADMIN)

    assert response.status_code == 200
    assert response.json()["config_store"] == {"writable": False}
    assert written.status_code == 409
    assert path.read_bytes() == before
    assert "ollama" in _names(response.json(), "chat")
