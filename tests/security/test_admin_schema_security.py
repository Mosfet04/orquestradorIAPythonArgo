"""FA-03, segurança de ``GET /admin/schema``: só a chave admin e nenhum valor de ambiente ou segredo no corpo.

Borda real (CORS + auth) com o schema do registry do app (``tests/fakes/admin_config_app.py``).
"""

from __future__ import annotations

import pytest

from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.dependency_injection import build_provider_registry
from tests.fakes import RecordingLogger
from tests.fakes.admin_config_app import ADMIN, ADMIN_KEY, RUN, RUN_KEY, admin_env, admin_world

SCHEMA = "/admin/schema"


async def test_schema_recusa_a_chave_run_com_403_e_request_sem_chave_com_401(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch) as world:
        with_run = await world.client.get(SCHEMA, headers=RUN)
        anonymous = await world.client.get(SCHEMA)
        with_admin = await world.client.get(SCHEMA, headers=ADMIN)

    assert with_run.status_code == 403
    assert with_run.json() == {"detail": "forbidden"}
    assert anonymous.status_code == 401
    assert with_admin.status_code == 200


async def test_corpo_nao_traz_valor_de_variavel_de_ambiente_nem_chave(monkeypatch: pytest.MonkeyPatch):
    admin_env(monkeypatch)
    secrets = {
        "OPENAI_API_KEY": "sk-segredo-openai-0123456789",
        "GEMINI_API_KEY": "segredo-gemini-0123456789",
        "SECRETS_DIR": "/segredo-dir-do-operador",
        "OLLAMA_BASE_URL": "http://ollama-do-operador.internal:11434",
        "MODEL_BASE_URL_ALLOWLIST": "llm-do-operador.internal",
    }
    for name, value in secrets.items():
        monkeypatch.setenv(name, value)
    registry = build_provider_registry(AppConfig.load(), RecordingLogger())

    async with admin_world(monkeypatch, providers=registry) as world:
        response = await world.client.get(SCHEMA, headers=ADMIN)

    assert response.status_code == 200
    leaked = [value for value in (*secrets.values(), ADMIN_KEY, RUN_KEY) if value in response.text]
    assert leaked == []
    assert "do-operador" not in response.text
