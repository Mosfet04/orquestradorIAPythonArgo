"""Container de injeção de dependências — Composition Root."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorClient

from src.application.services.agent_factory_service import AgentFactoryService
from src.application.services.embedder_model_factory_service import EmbedderModelFactory
from src.application.services.model_factory_service import ModelFactory
from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase
from src.domain.ports import ILogger
from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.http.http_tool_factory import HttpToolFactory
from src.infrastructure.logging.logger_adapter import StructlogLoggerAdapter
from src.infrastructure.repositories.mongo_agent_config_repository import (
    MongoAgentConfigRepository,
)
from src.infrastructure.repositories.mongo_tool_repository import MongoToolRepository
from src.presentation.controllers.orquestrador_controller import OrquestradorController


class HealthService:
    """Serviço de health check."""

    def __init__(self, mongo_client: AsyncIOMotorClient) -> None:
        self._mongo_client = mongo_client

    async def check_async(self) -> dict:
        start = asyncio.get_event_loop().time()
        checks = await asyncio.gather(
            self._check_mongodb(),
            self._check_memory(),
            return_exceptions=True,
        )
        elapsed = asyncio.get_event_loop().time() - start

        def _ok(c: Any) -> bool:
            return not isinstance(c, Exception) and c.get("status") not in (
                "error",
                "unhealthy",
            )

        return {
            "status": "healthy" if all(_ok(c) for c in checks) else "unhealthy",
            "checks": {
                "mongodb": checks[0] if not isinstance(checks[0], Exception) else {"status": "error", "error": str(checks[0])},
                "memory": checks[1] if not isinstance(checks[1], Exception) else {"status": "error", "error": str(checks[1])},
            },
            "response_time_ms": round(elapsed * 1000, 2),
        }

    async def _check_mongodb(self) -> dict:
        try:
            await self._mongo_client.admin.command("ping")
            return {"status": "healthy"}
        except Exception as exc:
            return {"status": "unhealthy", "error": str(exc)}

    @staticmethod
    async def _check_memory() -> dict:
        try:
            import psutil

            mem = psutil.virtual_memory()
            return {
                "status": "healthy" if mem.percent < 90 else "warning",
                "usage_percent": mem.percent,
                "available_gb": round(mem.available / (1024**3), 2),
            }
        except ImportError:
            return {"status": "unavailable", "message": "psutil não instalado"}


class DependencyContainer:
    """Composition Root — cria e fornece todas as dependências."""

    def __init__(self, config: AppConfig) -> None:
        self.config = config
        self._logger: ILogger = StructlogLoggerAdapter("app")
        self._mongo_client: Optional[AsyncIOMotorClient] = None
        self._health_service: Optional[HealthService] = None
        self._controller: Optional[OrquestradorController] = None

    @classmethod
    async def create_async(cls, config: AppConfig) -> DependencyContainer:
        container = cls(config)
        await container._initialize()
        return container

    async def _initialize(self) -> None:
        self._mongo_client = AsyncIOMotorClient(
            self.config.mongo_connection_string,
            maxPoolSize=50,
            minPoolSize=5,
            connectTimeoutMS=5000,
            serverSelectionTimeoutMS=5000,
        )
        try:
            await self._mongo_client.admin.command("ping")
        except Exception as exc:
            self._logger.warning(
                "MongoDB não disponível na inicialização", error=str(exc)
            )

        self._health_service = HealthService(self._mongo_client)

        # ── Wiring ──────────────────────────────────────────────────
        conn = self.config.mongo_connection_string
        db = self.config.mongo_database_name

        model_factory = ModelFactory(logger=self._logger)
        embedder_factory = EmbedderModelFactory(logger=self._logger)
        tool_factory = HttpToolFactory(logger=self._logger)

        agent_config_repo = MongoAgentConfigRepository(
            connection_string=conn, database_name=db, logger=self._logger
        )
        tool_repo = MongoToolRepository(
            connection_string=conn, database_name=db, logger=self._logger
        )

        agent_factory = AgentFactoryService(
            db_url=conn,
            db_name=db,
            logger=self._logger,
            model_factory=model_factory,
            embedder_factory=embedder_factory,
            tool_factory=tool_factory,
            tool_repository=tool_repo,
        )

        use_case = GetActiveAgentsUseCase(agent_factory, agent_config_repo)

        self._controller = OrquestradorController(
            get_active_agents_use_case=use_case,
            logger=self._logger,
        )

    def get_orquestrador_controller(self) -> OrquestradorController:
        assert self._controller is not None, "Container não inicializado"
        return self._controller

    @property
    def health_service(self) -> Optional[HealthService]:
        return self._health_service

    async def cleanup(self) -> None:
        if self._mongo_client:
            try:
                result: Any = self._mongo_client.close()
                if asyncio.iscoroutine(result):
                    await result
            except Exception:
                pass