"""Factory para criação da aplicação FastAPI com AgentOS (agno v2.5)."""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Optional

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.dependency_injection import DependencyContainer
from src.infrastructure.logging.logger_adapter import StructlogLoggerAdapter


class AppFactory:
    """Cria e configura a aplicação FastAPI + AgentOS."""

    def __init__(self) -> None:
        self._container: Optional[DependencyContainer] = None
        self._logger = StructlogLoggerAdapter("app_factory")

    async def create_app(self) -> FastAPI:
        """Cria a aplicação FastAPI — ponto de entrada único."""
        try:
            base_app = FastAPI(
                title="Orquestrador de Agentes IA",
                description="Sistema de orquestração de agentes IA",
                version="2.0.0",
                lifespan=self._lifespan,
            )

            self._add_cors(base_app)
            self._add_admin_endpoints(base_app)

            # Inicializar container se necessário
            if not self._container:
                config = AppConfig.load()
                self._container = await DependencyContainer.create_async(config)

            # Carregar agentes e criar AgentOS
            controller = self._container.get_orquestrador_controller()
            agents = await controller.get_agents()

            if agents:
                from agno.os import AgentOS

                agent_os = AgentOS(
                    agents=agents,
                    cors_allowed_origins=["*"],
                    base_app=base_app,
                    on_route_conflict="preserve_base_app",
                )
                return agent_os.get_app()

            return base_app

        except Exception as exc:
            self._logger.error(
                "Erro crítico ao criar aplicação",
                error_type=exc.__class__.__name__,
                error=str(exc),
            )
            raise

    # ── middleware ───────────────────────────────────────────────────

    @staticmethod
    def _add_cors(app: FastAPI) -> None:
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["*"],
            allow_headers=["*"],
            expose_headers=["*"],
        )

    # ── admin endpoints ─────────────────────────────────────────────

    def _add_admin_endpoints(self, app: FastAPI) -> None:
        @app.get("/health")
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

    @asynccontextmanager
    async def _lifespan(self, app: FastAPI):
        try:
            if not self._container:
                config = AppConfig.load()
                self._container = await DependencyContainer.create_async(config)

            controller = self._container.get_orquestrador_controller()
            await controller.warm_up_cache()
            yield
        finally:
            if self._container:
                await self._container.cleanup()


# ── module-level factory ────────────────────────────────────────────

_factory = AppFactory()


async def create_app() -> FastAPI:
    """Factory assíncrona global."""
    return await _factory.create_app()
