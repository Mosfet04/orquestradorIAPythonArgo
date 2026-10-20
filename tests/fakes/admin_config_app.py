"""Borda real do app (CORS + auth por chave) com as rotas ``/admin/config`` e ``/admin/schema`` em memória.

``admin_world`` cria o app pelo ``AppFactory`` com os routers de ``src/presentation`` e põe no
``app.state`` o caso de uso montado como o container monta (``build_config_admin``, mappers reais do
store, troca a quente sobre o ``FakeAgentRuntime``, diário de escritas do container sobre o Mongo falso) e
o schema do registry recebido (``build_config_schema``; default: os built-ins). Sem lifespan: o
``httpx.ASGITransport`` não o dispara. Chaves são valores de teste.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

import httpx
import pytest
from fastapi import FastAPI

from src.application.use_cases.config_admin_use_case import ConfigAdmin, RuntimeSwap
from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase
from src.application.use_cases.get_active_teams_use_case import GetActiveTeamsUseCase
from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.team_config import TeamConfig
from src.domain.entities.tool import HttpMethod, ParameterType, Tool, ToolParameter
from src.domain.ports import AgentRuntime
from src.domain.repositories.agent_config_repository import IAgentConfigRepository
from src.domain.repositories.team_config_repository import ITeamConfigRepository
from src.domain.repositories.tool_repository import IToolRepository
from src.infrastructure.dependency_injection import build_config_admin, build_config_schema
from src.infrastructure.providers import ProviderRegistry
from src.infrastructure.providers.builtins import BUILTIN_PROVIDERS
from src.infrastructure.repositories.mongo_config_audit_log import MongoConfigAuditLog
from src.infrastructure.web.app_factory import AppFactory
from src.presentation.routes.admin_config import router as admin_config_router
from src.presentation.routes.admin_schema import router as admin_schema_router
from tests.fakes.logger import RecordingLogger
from tests.fakes.mongo import FakeMotorClient
from tests.fakes.repositories import (
    InMemoryAgentConfigRepository,
    InMemoryTeamConfigRepository,
    InMemoryToolRepository,
)
from tests.fakes.runtime import FakeAgentRuntime
from tests.fakes.startup_world import CLEAN_ENV
from tests.fakes.web import LOOPBACK_BASE_URL, LOOPBACK_CLIENT

RUN_KEY = "fa02-run-key-" + "r" * 20
ADMIN_KEY = "fa02-admin-key-" + "a" * 19
RUN = {"Authorization": f"Bearer {RUN_KEY}"}
ADMIN = {"Authorization": f"Bearer {ADMIN_KEY}"}


def agent(agent_id: str, *, tools_ids: list[str] | None = None, active: bool = True) -> AgentConfig:
    return AgentConfig(
        id=agent_id,
        nome=f"Agente {agent_id}",
        factory_ia_model="ollama",
        model="llama3.2",
        descricao="d",
        prompt="p",
        tools_ids=tools_ids or [],
        active=active,
    )


def team(team_id: str, member_ids: list[str]) -> TeamConfig:
    return TeamConfig(
        id=team_id, nome=f"Time {team_id}", factory_ia_model="ollama", model="qwen3", member_ids=member_ids
    )


def tool(tool_id: str, *, headers: dict[str, str] | None = None) -> Tool:
    return Tool(
        id=tool_id,
        name=tool_id,
        description="Busca endereço",
        route="https://api.example.invalid/cep/{cep}",
        http_method=HttpMethod.GET,
        parameters=[ToolParameter(name="cep", type=ParameterType.STRING, description="CEP")],
        headers=headers,
    )


def agent_body(agent_id: str, **extra: Any) -> dict[str, Any]:
    body: dict[str, Any] = {
        "id": agent_id,
        "nome": f"Agente {agent_id}",
        "factory_ia_model": "ollama",
        "model": "llama3.2",
        "prompt": "p",
        "tools_ids": [],
    }
    body.update(extra)
    return body


def tool_body(**extra: Any) -> dict[str, Any]:
    """Corpo da tool ``cep`` (sem parâmetros declarados); ``extra`` troca ou acrescenta campos (ex.: ``headers``)."""
    body: dict[str, Any] = {
        "id": "cep",
        "name": "cep",
        "description": "Busca CEP",
        "route": "https://api.example.invalid/{cep}",
        "http_method": "GET",
        "parameters": [],
    }
    body.update(extra)
    return body


def team_body(team_id: str, member_ids: list[str]) -> dict[str, Any]:
    return {"id": team_id, "nome": team_id, "factory_ia_model": "ollama", "model": "qwen3", "member_ids": member_ids}


def if_match(version: int | str) -> dict[str, str]:
    """Headers de admin com o ``If-Match`` da versão (entre aspas, como o ``ETag``)."""
    return {**ADMIN, "If-Match": f'"{version}"'}


def served_ids(runtime: FakeAgentRuntime) -> tuple[list[str], list[str]]:
    """Ids dos agents e teams que o runtime falso serve agora."""
    agents, teams = runtime.served
    return [a.id for a in agents], [t.id for t in teams]


def fake_runtime_swap(
    agents: IAgentConfigRepository,
    teams: ITeamConfigRepository,
    runtime: AgentRuntime | None = None,
    logger: RecordingLogger | None = None,
) -> RuntimeSwap:
    """Troca a quente como o container monta, sobre ``runtime`` (default: um ``FakeAgentRuntime`` novo).

    ``logger`` é o da carga e do reload; o default é um próprio, fora do log do caso de uso de config.
    """
    runtime = runtime or FakeAgentRuntime()
    logger = logger or RecordingLogger()
    return RuntimeSwap(
        runtime,
        GetActiveAgentsUseCase(runtime, agents, logger),
        GetActiveTeamsUseCase(runtime, teams, logger),
        logger,
    )


def fake_audit_log(database: Any = None) -> MongoConfigAuditLog:
    """Diário de escritas do container (FA-05) sobre o Mongo falso; ``database`` compartilhado = mesmo Mongo."""
    return MongoConfigAuditLog(database if database is not None else FakeMotorClient()["agno"])


def fake_config_admin(
    agents: IAgentConfigRepository,
    teams: ITeamConfigRepository,
    tools: IToolRepository,
    logger: RecordingLogger,
    runtime: AgentRuntime | None = None,
    audit: MongoConfigAuditLog | None = None,
) -> ConfigAdmin:
    """``build_config_admin`` com a troca a quente sobre ``runtime`` (default: um ``FakeAgentRuntime`` novo).

    ``audit`` é o diário das escritas (default: um novo, num Mongo falso só dele).
    """
    return build_config_admin(
        agents, teams, tools, logger, runtime=fake_runtime_swap(agents, teams, runtime), audit=audit or fake_audit_log()
    )


def read_only_config_admin() -> ConfigAdmin:
    """Caso de uso sem diário (store só leitura, como o YAML) sobre repositórios vazios.

    Para os containers falsos que só sobem o lifespan: sem diário, ele não lê revisão nem liga o poll.
    """
    agents, teams = InMemoryAgentConfigRepository(), InMemoryTeamConfigRepository()
    return build_config_admin(
        agents, teams, InMemoryToolRepository(), RecordingLogger(), runtime=fake_runtime_swap(agents, teams), audit=None
    )


@dataclass
class AdminWorld:
    agents: InMemoryAgentConfigRepository
    teams: InMemoryTeamConfigRepository
    tools: InMemoryToolRepository
    logger: RecordingLogger
    app: FastAPI
    client: httpx.AsyncClient
    runtime: FakeAgentRuntime
    audit_db: Any
    """Database falso do diário (``config_audit``, ``config_revision``): o que ficou gravado, cru."""


def admin_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Ambiente de produção em loopback com as duas chaves de teste (run e admin), sem o resto da config."""
    for name in CLEAN_ENV:
        monkeypatch.delenv(name, raising=False)
    for name, value in {
        "ENVIRONMENT": "production",
        "APP_HOST": "127.0.0.1",
        "API_KEY_RUN": RUN_KEY,
        "API_KEY_ADMIN": ADMIN_KEY,
    }.items():
        monkeypatch.setenv(name, value)


@asynccontextmanager
async def admin_world(
    monkeypatch: pytest.MonkeyPatch,
    *,
    agents: Iterable[AgentConfig] = (),
    teams: Iterable[TeamConfig] = (),
    tools: Iterable[Tool] = (),
    providers: ProviderRegistry | None = None,
    runtime: FakeAgentRuntime | None = None,
) -> AsyncIterator[AdminWorld]:
    admin_env(monkeypatch)
    logger = RecordingLogger()
    agent_repo = InMemoryAgentConfigRepository(agents)
    team_repo = InMemoryTeamConfigRepository(teams)
    tool_repo = InMemoryToolRepository(tools)
    runtime = runtime or FakeAgentRuntime()
    audit_db = FakeMotorClient()["agno"]
    audit = fake_audit_log(audit_db)
    app = AppFactory().create_app(routers=[admin_config_router, admin_schema_router])
    app.state.config_admin = fake_config_admin(agent_repo, team_repo, tool_repo, logger, runtime, audit)
    app.state.config_schema = build_config_schema(providers or ProviderRegistry(BUILTIN_PROVIDERS), writable=True)
    transport = httpx.ASGITransport(app=app, client=LOOPBACK_CLIENT)
    async with httpx.AsyncClient(transport=transport, base_url=LOOPBACK_BASE_URL) as client:
        yield AdminWorld(agent_repo, team_repo, tool_repo, logger, app, client, runtime, audit_db)
