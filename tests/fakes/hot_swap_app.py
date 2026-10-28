"""Pilha completa da troca a quente (FA-04): escrita pela API -> build -> persist -> swap no ``AgnoRuntime`` real.

``hot_stack`` sobe o ``AppFactory`` (lifespan real) -> container -> repositórios Mongo sobre a coleção falsa
(``tests/fakes/startup_world.py``) -> ``AgnoRuntime`` -> AgentOS e AG-UI, com o router ``/admin/config``.
O modelo é um ``GatedModel`` por montagem (cada agente montado ganha o seu): as instruções que ele recebeu
mostram qual instância do agente atendeu o run, e ``gate`` segura um run em stream no meio. ``store`` dá
acesso às coleções falsas (documentos mudados por fora da API; falha injetada no driver).
``replica_stacks`` sobe várias pilhas (réplicas, FA-05) no mesmo Mongo falso; ``hot_stack`` é uma só.
Chaves são valores de teste.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import httpx
import pytest
from agno.models.response import ModelResponse
from fastapi import FastAPI

from src.domain.ports import InvalidModelConfigError
from src.infrastructure import dependency_injection as di
from src.infrastructure.repositories import mongo_base
from src.infrastructure.web import app_factory
from src.presentation.routes.admin_config import router as admin_config_router
from tests.fakes.admin_config_app import ADMIN, RUN, admin_env
from tests.fakes.models import FakeChatModel
from tests.fakes.mongo import FakeMongoCollection
from tests.fakes.startup_world import Events, startup_world
from tests.fakes.web import LOOPBACK_BASE_URL, LOOPBACK_CLIENT

CONFIG = "/admin/config"
POLL_SECONDS = 0.05
"""Ciclo do poll nos testes que o ligam (``replica_stacks(..., poll_seconds=str(POLL_SECONDS))``)."""
REFUSED_MODEL = "modelo-recusado"
"""``model`` que a fábrica de modelos falsa recusa (``InvalidModelConfigError``): config que não monta."""


@dataclass
class Gate:
    """Segura o run em stream no meio (o modelo já foi chamado) até ``release``."""

    armed: bool = False
    reached: asyncio.Event = field(default_factory=asyncio.Event)
    release: asyncio.Event = field(default_factory=asyncio.Event)


@dataclass
class GatedModel(FakeChatModel):
    gate: Gate | None = None

    async def ainvoke_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ModelResponse]:
        response = self._next_response(*args, **kwargs)
        if self.gate is not None and self.gate.armed:
            self.gate.reached.set()
            await self.gate.release.wait()
        yield response


@dataclass
class HotStack:
    client: httpx.AsyncClient
    gate: Gate
    models: list[GatedModel]
    store: dict[str, FakeMongoCollection]
    app: FastAPI

    def instructions_seen(self) -> list[str]:
        """Mensagem de sistema de cada chamada ao modelo, na ordem das montagens."""
        return [content for model in self.models for call in model.calls for role, content in call.messages
                if role == "system"]

    async def revision(self) -> int:
        """``runtime_revision`` do ``/admin/health``."""
        health = await self.client.get("/admin/health", headers=ADMIN)
        return int(health.json()["runtime_revision"])


@asynccontextmanager
async def hot_stack(monkeypatch: pytest.MonkeyPatch, *, raise_app_exceptions: bool = True) -> AsyncIterator[HotStack]:
    """A pilha; com ``raise_app_exceptions=False`` o erro do app vira a resposta 500 em vez de subir ao teste."""
    async with replica_stacks(monkeypatch, count=1, raise_app_exceptions=raise_app_exceptions) as (stack,):
        yield stack


class Replicas(list[HotStack]):
    """As réplicas de ``replica_stacks``; ``add()`` sobe mais uma, no mesmo Mongo falso (réplica que chega tarde)."""

    def __init__(self, start: Callable[[], Awaitable[HotStack]]) -> None:
        super().__init__()
        self._start = start

    async def add(self) -> HotStack:
        """Sobe mais uma réplica (lifespan real) e a acrescenta à lista."""
        replica = await self._start()
        self.append(replica)
        return replica


@asynccontextmanager
async def replica_stacks(
    monkeypatch: pytest.MonkeyPatch, *, count: int, poll_seconds: str = "3600", raise_app_exceptions: bool = True
) -> AsyncIterator[Replicas]:
    """``count`` réplicas (um ``AppFactory`` e um lifespan cada) no mesmo Mongo falso, com o mesmo ``gate``.

    ``poll_seconds`` é o ``CONFIG_REVISION_POLL_SECONDS``: o default (uma hora) deixa o poll do lifespan parado,
    e o teste dá uma volta dele chamando ``app.state.config_admin.sync()``.
    """
    admin_env(monkeypatch)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    monkeypatch.setenv("CONFIG_REVISION_POLL_SECONDS", poll_seconds)
    gate = Gate()
    models: list[GatedModel] = []

    def create_model(self: Any, config: Any) -> GatedModel:
        if config.model_id == REFUSED_MODEL:
            raise InvalidModelConfigError("modelo recusado pela fábrica")
        model = GatedModel(id=config.model_id, responses=[f"resposta da montagem {len(models) + 1}"] * 5, gate=gate)
        models.append(model)
        return model

    with startup_world(Events(), agents=()), patch.object(di.ProviderRegistry, "create_model", create_model):
        store = mongo_base.MongoClientFactory.get_client("hot-swap")["hot-swap"]
        async with AsyncExitStack() as stack:
            async def start() -> HotStack:
                app = app_factory.AppFactory().create_app(routers=[admin_config_router])
                await stack.enter_async_context(app.router.lifespan_context(app))
                transport = httpx.ASGITransport(
                    app=app, client=LOOPBACK_CLIENT, raise_app_exceptions=raise_app_exceptions
                )
                client = await stack.enter_async_context(
                    httpx.AsyncClient(transport=transport, base_url=LOOPBACK_BASE_URL)
                )
                return HotStack(client, gate, models, store, app)

            replicas = Replicas(start)
            for _ in range(count):
                await replicas.add()
            yield replicas


async def served_within(
    client: httpx.AsyncClient, path: str, *, status: int = 200, seconds: float = 5
) -> httpx.Response:
    """O primeiro ``GET path`` com ``status`` dentro de ``seconds`` (a última resposta, se não vier).

    Espera o poll do lifespan (``POLL_SECONDS``); a folga é para a CI, o ciclo é bem menor.
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + seconds
    while True:
        response = await client.get(path, headers=RUN)
        if response.status_code == status or loop.time() >= deadline:
            return response
        await asyncio.sleep(POLL_SECONDS / 5)


def agui_body(run_id: str, *, user_id: str | None = None) -> dict[str, Any]:
    """Corpo do ``POST /agui/{id}``; ``user_id`` vai em ``forwardedProps`` (teams com memória o exigem)."""
    return {
        "threadId": f"thread-{run_id}",
        "runId": run_id,
        "state": {},
        "messages": [{"id": f"m-{run_id}", "role": "user", "content": "oi"}],
        "tools": [],
        "context": [],
        "forwardedProps": {} if user_id is None else {"user_id": user_id},
    }


async def run_rest(client: httpx.AsyncClient, entity_id: str) -> httpx.Response:
    """Run não-stream de agente pelo AgentOS."""
    return await client.post(f"/agents/{entity_id}/runs", data={"message": "oi", "stream": "false"}, headers=RUN)
