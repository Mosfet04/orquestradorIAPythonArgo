"""App real com ``CONFIG_STORE=yaml`` para testes de ponta a ponta (QA do F2-06).

Pilha: ``AppFactory`` (lifespan) -> container -> repositórios YAML -> use cases -> ``AgnoRuntime`` ->
AgentOS, sobre ``tests/fakes/startup_world.py`` (Mongo, cliente do db de sessões e modelo falsos). O
modelo é um ``FakeChatModel`` roteirizado por ``model_id`` e a tool HTTP do YAML fala com um
``httpx.MockTransport`` (nada sai da máquina). Chaves são valores de teste.
"""

from __future__ import annotations

from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from agno.models.response import ModelResponse
from fastapi import APIRouter
from starlette.testclient import TestClient

from src.infrastructure import dependency_injection as di
from src.infrastructure.web import app_factory
from tests.fakes.logger import RecordingLogger
from tests.fakes.models import FakeChatModel
from tests.fakes.startup_world import CLEAN_ENV, Events, startup_world
from tests.fakes.web import LOOPBACK_BASE_URL, LOOPBACK_CLIENT

RUN_KEY = "qa6-run-key-" + "r" * 21
ADMIN_KEY = "qa6-admin-key-" + "a" * 19
RUN = {"Authorization": f"Bearer {RUN_KEY}"}
ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}
BASE = "https://api.example.invalid"


class YamlWorld:
    """O que o app do teste recebeu: modelos por ``model_id``, requests da tool HTTP e logs."""

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
    routers: Sequence[APIRouter] = (),
) -> Iterator[tuple[YamlWorld, Path, TestClient]]:
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

    world = YamlWorld()
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
        app = app_factory.AppFactory().create_app(routers=routers)
        with TestClient(app, base_url=LOOPBACK_BASE_URL, client=LOOPBACK_CLIENT, raise_server_exceptions=False) as http:
            yield world, path, http
