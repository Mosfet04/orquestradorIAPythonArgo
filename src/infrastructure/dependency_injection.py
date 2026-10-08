"""Container de injeção de dependências — Composition Root."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorClient

from src.application.services.agent_factory_service import AgentFactoryService
from src.application.services.document_indexing_service import DocumentIndexingService
from src.application.services.embedder_model_factory_service import EmbedderModelFactory
from src.application.services.knowledge_search_factory import KnowledgeSearchFactory
from src.application.services.model_factory_service import ModelFactory
from src.application.services.team_factory_service import TeamFactoryService
from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase
from src.application.use_cases.get_active_teams_use_case import GetActiveTeamsUseCase
from src.domain.ports import ILogger
from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.http.http_tool_factory import HttpToolFactory
from src.infrastructure.logging.logger_adapter import StructlogLoggerAdapter
from src.infrastructure.parsers.text_document_parser import TextDocumentParser
from src.infrastructure.repositories.mongo_agent_config_repository import (
    MongoAgentConfigRepository,
)
from src.infrastructure.repositories.mongo_document_tree_repository import (
    MongoDocumentTreeRepository,
)
from src.infrastructure.repositories.mongo_team_config_repository import (
    MongoTeamConfigRepository,
)
from src.infrastructure.repositories.mongo_tool_repository import MongoToolRepository
from src.infrastructure.services.llm_summary_generator import LLMSummaryGenerator
from src.presentation.controllers.orquestrador_controller import OrquestradorController


class HealthService:
    """Serviço de health check.

    O corpo devolvido vai ao cliente HTTP: só status e métricas, nunca texto de exceção.
    O detalhe da falha fica no log interno, pelo tipo da exceção.
    """

    _CHECK_NAMES = ("mongodb", "memory", "otlp")

    def __init__(self, mongo_client: AsyncIOMotorClient, logger: ILogger) -> None:
        self._mongo_client = mongo_client
        self._logger = logger

    async def check_async(self) -> dict:
        start = asyncio.get_event_loop().time()
        checks = await asyncio.gather(
            self._check_mongodb(),
            self._check_memory(),
            self._check_otlp(),
            return_exceptions=True,
        )
        elapsed = asyncio.get_event_loop().time() - start

        results: dict[str, dict] = {}
        for name, check in zip(self._CHECK_NAMES, checks, strict=True):
            if isinstance(check, BaseException):
                self._logger.error(
                    "Health check falhou com exceção inesperada",
                    check=name,
                    error_type=type(check).__name__,
                )
                results[name] = {"status": "error"}
            else:
                results[name] = check

        healthy = all(r.get("status") not in ("error", "unhealthy") for r in results.values())
        return {
            "status": "healthy" if healthy else "unhealthy",
            "checks": results,
            "response_time_ms": round(elapsed * 1000, 2),
        }

    async def _check_mongodb(self) -> dict:
        try:
            await self._mongo_client.admin.command("ping")
            return {"status": "healthy"}
        except Exception as exc:
            self._logger.warning(
                "Health check: MongoDB indisponível", error_type=type(exc).__name__
            )
            return {"status": "unhealthy"}

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

    @staticmethod
    async def _check_otlp() -> dict:
        """Informa se há um TracerProvider de SDK configurado (OTLP ligado).

        Sem ``force_flush``: ele é síncrono (bloquearia o event loop por até 2 s), exporta
        spans como efeito colateral e o retorno nunca era usado, então não media alcance.
        """
        from opentelemetry import trace

        provider = trace.get_tracer_provider()
        if hasattr(provider, "force_flush"):
            return {"status": "configured", "provider": type(provider).__name__}
        return {"status": "not_configured"}


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

        self._health_service = HealthService(self._mongo_client, self._logger)

        # ── Wiring ──────────────────────────────────────────────────
        conn = self.config.mongo_connection_string
        db = self.config.mongo_database_name

        ollama_host = self.config.ollama_base_url
        model_factory = ModelFactory(logger=self._logger, ollama_host=ollama_host)
        embedder_factory = EmbedderModelFactory(
            logger=self._logger, ollama_host=ollama_host
        )
        tool_factory = HttpToolFactory(logger=self._logger)

        agent_config_repo = MongoAgentConfigRepository(
            connection_string=conn, database_name=db, logger=self._logger
        )
        tool_repo = MongoToolRepository(
            connection_string=conn, database_name=db, logger=self._logger
        )

        # ── Hierárquica: parser, tree repo, summary gen, factories ──
        doc_parser = TextDocumentParser()
        tree_repo = MongoDocumentTreeRepository(
            connection_string=conn, database_name=db, logger=self._logger
        )
        try:
            await tree_repo.ensure_indexes()
        except Exception as exc:
            self._logger.warning(
                "Não foi possível criar índices da árvore de documentos",
                error=str(exc),
            )

        summary_generator = LLMSummaryGenerator(
            model_factory=model_factory, logger=self._logger
        )
        indexing_service = DocumentIndexingService(
            parser=doc_parser,
            tree_repository=tree_repo,
            summary_generator=summary_generator,
            embedder_factory=embedder_factory,
            logger=self._logger,
        )
        search_factory = KnowledgeSearchFactory(
            tree_repository=tree_repo, logger=self._logger
        )

        agent_factory = AgentFactoryService(
            db_url=conn,
            db_name=db,
            logger=self._logger,
            model_factory=model_factory,
            embedder_factory=embedder_factory,
            tool_factory=tool_factory,
            tool_repository=tool_repo,
            indexing_service=indexing_service,
            search_factory=search_factory,
        )

        team_factory = TeamFactoryService(
            db_url=conn,
            db_name=db,
            logger=self._logger,
            model_factory=model_factory,
        )

        team_config_repo = MongoTeamConfigRepository(
            connection_string=conn, database_name=db, logger=self._logger
        )

        agents_use_case = GetActiveAgentsUseCase(agent_factory, agent_config_repo)
        teams_use_case = GetActiveTeamsUseCase(
            team_factory, team_config_repo, self._logger
        )

        self._controller = OrquestradorController(
            get_active_agents_use_case=agents_use_case,
            get_active_teams_use_case=teams_use_case,
            logger=self._logger,
        )

    def get_orquestrador_controller(self) -> OrquestradorController:
        if self._controller is None:
            raise RuntimeError("Container não inicializado: chame create_async() antes")
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
            except Exception as exc:
                # Shutdown segue mesmo se o driver falhar ao fechar; o tipo vai ao log.
                self._logger.warning(
                    "Falha ao fechar o cliente MongoDB", error_type=type(exc).__name__
                )
