"""Factory para criação da aplicação FastAPI com AgentOS (agno v2.5)."""

from __future__ import annotations

import re
from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from starlette.middleware.base import BaseHTTPMiddleware

from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.dependency_injection import DependencyContainer
from src.infrastructure.logging.logger_adapter import StructlogLoggerAdapter
from agno.os import AgentOS
from agno.os.interfaces.agui import AGUI
# Regex: /agents/{agent_id}/sessions/… → /sessions/…
_AGENT_SESSION_RE = re.compile(r"^/agents/[^/]+(/sessions/.*)$")


class _PlaygroundPrefixMiddleware(BaseHTTPMiddleware):
    """Reescreve paths para compatibilidade com app.agno.com.

    1. ``/playground/…`` → ``/…``  (app.agno.com prefixa tudo com /playground)
    2. ``/agents/{id}/sessions/…`` → ``/sessions/…``  (AgentOS registra sessões
       na raiz, mas o frontend as coloca sob o agente)
    """

    async def dispatch(self, request: Request, call_next):
        path: str = request.scope.get("path", "")
        # 1) Strip /playground prefix
        if path.startswith("/playground/"):
            path = path[len("/playground"):]
            request.scope["path"] = path
        # 2) Rewrite /agents/{id}/sessions/… → /sessions/…
        m = _AGENT_SESSION_RE.match(path)
        if m:
            request.scope["path"] = m.group(1)
        return await call_next(request)


class AppFactory:
    """Cria e configura a aplicação FastAPI + AgentOS.

    A app FastAPI é criada **sincronamente** para que ``uvicorn`` receba
    um objeto ASGI real (não uma coroutine).  Toda inicialização async
    (DI, agentes, AgentOS) acontece dentro do *lifespan*.
    """

    _ALLOWED_ORIGINS = [
        "https://app.agno.com",
        "https://www.agno.com",
        "http://localhost:3000",
        "http://localhost:7777",
        "https://os.agno.com"
    ]

    def __init__(self) -> None:
        self._container: Optional[DependencyContainer] = None
        self._logger = StructlogLoggerAdapter("app_factory")

    def create_app(self) -> FastAPI:
        """Cria a aplicação FastAPI — **síncrono** (module-level safe)."""
        base_app = FastAPI(
            title="Orquestrador de Agentes IA",
            description="Sistema de orquestração de agentes IA",
            version="2.0.0",
            lifespan=self._lifespan,
        )

        self._add_cors(base_app)
        #self._add_playground_rewrite(base_app)
        self._add_admin_endpoints(base_app)
        #self._add_playground_compat_endpoints(base_app)
        return base_app

    # ── middleware ───────────────────────────────────────────────────

    @staticmethod
    def _add_playground_rewrite(app: FastAPI) -> None:
        """Reescreve /playground/* → /* (compatibilidade app.agno.com)."""
        app.add_middleware(_PlaygroundPrefixMiddleware)

    @classmethod
    def _add_cors(cls, app: FastAPI) -> None:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=cls._ALLOWED_ORIGINS,
            allow_credentials=True,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["*"],
        )

    # ── admin endpoints ─────────────────────────────────────────────

    def _add_admin_endpoints(self, app: FastAPI) -> None:
        @app.get("/admin/health")
        async def health_check():
            if self._container and self._container.health_service:
                return await self._container.health_service.check_async()
            return {"status": "healthy"}

        @app.get("/metrics/cache")
        async def cache_metrics():
            if self._container:
                ctrl = self._container.get_orquestrador_controller()
                return ctrl.get_cache_stats()
            return {"status": "no_cache"}

        @app.post("/admin/refresh-cache")
        async def refresh_cache():
            if self._container:
                ctrl = self._container.get_orquestrador_controller()
                await ctrl.refresh_agents()
                return {"status": "cache_refreshed"}
            return {"status": "no_cache"}

    # ── lifespan ────────────────────────────────────────────────────

    async def _ensure_container(self) -> None:
        """Garante que o DependencyContainer esteja inicializado."""
        if self._container:
            return
        self._logger.info("Lifespan: carregando AppConfig...")
        config = AppConfig.load()
        self._logger.info(
            "Lifespan: criando DependencyContainer...",
            mongo_db=config.mongo_database_name,
        )
        self._container = await DependencyContainer.create_async(config)
        self._logger.info("Lifespan: container criado com sucesso")

    async def _load_agents(self):
        """Aquece cache e retorna lista de agentes ativos."""
        controller = self._container.get_orquestrador_controller()
        self._logger.info("Lifespan: warm up cache...")
        await controller.warm_up_cache()

        self._logger.info("Lifespan: carregando agentes...")
        agents = await controller.get_agents()
        self._logger.info(
            "Lifespan: agentes carregados",
            agent_count=len(agents) if agents else 0,
            agent_ids=[a.id for a in agents] if agents else [],
        )
        return agents

    async def _load_teams(self):
        """Carrega teams ativos (dependem dos agentes em cache)."""
        controller = self._container.get_orquestrador_controller()
        teams = await controller.get_teams()
        self._logger.info(
            "Lifespan: teams carregados",
            team_count=len(teams) if teams else 0,
            team_ids=[t.id for t in teams] if teams else [],
        )
        return teams

    def _mount_agent_os(self, app: FastAPI, agents: list, teams: list) -> None:
        """Cria interfaces AG-UI e monta o AgentOS no app base."""
        agent_interfaces = [AGUI(agent=agent) for agent in agents]
        team_interfaces = [AGUI(team=team) for team in teams]
        interfaces = agent_interfaces + team_interfaces
        self._logger.info(
            "Lifespan: montando AgentOS com interfaces AG-UI",
            interface_count=len(interfaces),
        )
        agent_os = AgentOS(
            agents=agents,
            teams=teams or None,
            interfaces=interfaces,
            cors_allowed_origins=self._ALLOWED_ORIGINS,
            base_app=app,
            on_route_conflict="preserve_base_app",
            tracing=True
        )
        agent_os.get_app()
        app.openapi_schema = None
        self._logger.info(
            "AgentOS montado com sucesso",
            agent_count=len(agents),
            team_count=len(teams),
            total_routes=len(app.routes),
        )

    @asynccontextmanager
    async def _lifespan(self, app: FastAPI):
        """Inicializa DI + agentes e, se houver agentes, monta AgentOS."""
        try:
            self._logger.info("Lifespan: iniciando...")
            await self._ensure_container()
            agents = await self._load_agents()
            teams = await self._load_teams()

            if agents or teams:
                try:
                    self._mount_agent_os(app, agents or [], teams or [])
                except Exception as exc:
                    self._logger.error(
                        "Erro ao montar AgentOS — continuando sem rotas de agente",
                        error_type=exc.__class__.__name__,
                        error=str(exc),
                    )
            else:
                self._logger.info("Nenhum agente ou team ativo — rodando só endpoints admin")

            yield
        except Exception as exc:
            self._logger.error(
                "Erro crítico no lifespan",
                error_type=exc.__class__.__name__,
                error=str(exc),
            )
            raise
        finally:
            if self._container:
                await self._container.cleanup()


# ── module-level factory ────────────────────────────────────────────

_factory = AppFactory()


def create_app() -> FastAPI:
    """Factory **síncrona** global — segura para ``app = create_app()``."""
    return _factory.create_app()
