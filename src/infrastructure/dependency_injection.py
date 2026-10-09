"""Container de injeção de dependências — Composition Root."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from motor.motor_asyncio import AsyncIOMotorClient

from src.application.services.agent_factory_service import AgentFactoryService
from src.application.services.document_indexing_service import DocumentIndexingService
from src.application.services.knowledge_search_factory import KnowledgeSearchFactory
from src.application.services.team_factory_service import TeamFactoryService
from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase
from src.application.use_cases.get_active_teams_use_case import GetActiveTeamsUseCase
from src.domain.ports import ILogger
from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.http.http_tool_factory import HttpToolFactory
from src.infrastructure.logging.logger_adapter import StructlogLoggerAdapter
from src.infrastructure.parsers.text_document_parser import TextDocumentParser
from src.infrastructure.providers import DestinationPolicy, ProviderRegistry
from src.infrastructure.providers.builtins import BUILTIN_PROVIDERS
from src.infrastructure.providers.plugins import load_provider_plugins
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
from src.infrastructure.web.api_key_auth import is_local_dev_mode
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
        # Providers primeiro: plugin recusado ou id duplicado derruba o startup antes do Mongo
        # (e antes do bind: o uvicorn só abre o socket depois do startup do lifespan). Em
        # thread: descobrir entry points lê metadados em disco e carregar plugin importa código.
        # Um registry atende modelos e embedders (portas IModelFactory e IEmbedderFactory).
        providers = await asyncio.to_thread(self._build_provider_registry)

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
            model_factory=providers, logger=self._logger
        )
        indexing_service = DocumentIndexingService(
            parser=doc_parser,
            tree_repository=tree_repo,
            summary_generator=summary_generator,
            embedder_factory=providers,
            logger=self._logger,
        )
        search_factory = KnowledgeSearchFactory(
            tree_repository=tree_repo, logger=self._logger
        )

        agent_factory = AgentFactoryService(
            db_url=conn,
            db_name=db,
            logger=self._logger,
            model_factory=providers,
            embedder_factory=providers,
            tool_factory=tool_factory,
            tool_repository=tool_repo,
            indexing_service=indexing_service,
            search_factory=search_factory,
        )

        team_factory = TeamFactoryService(
            db_url=conn,
            db_name=db,
            logger=self._logger,
            model_factory=providers,
        )

        team_config_repo = MongoTeamConfigRepository(
            connection_string=conn, database_name=db, logger=self._logger
        )

        agents_use_case = GetActiveAgentsUseCase(
            agent_factory, agent_config_repo, self._logger
        )
        teams_use_case = GetActiveTeamsUseCase(
            team_factory, team_config_repo, self._logger
        )

        self._controller = OrquestradorController(
            get_active_agents_use_case=agents_use_case,
            get_active_teams_use_case=teams_use_case,
            logger=self._logger,
        )

    def _build_provider_registry(self) -> ProviderRegistry:
        """Built-ins + plugins + segredos em ``SECRETS_DIR`` + destino permitido para ``base_url``.

        ``OLLAMA_BASE_URL`` é do operador: vai só para o Ollama e não passa pela allowlist.
        Loopback em ``base_url`` e import dinâmico de spec só no modo dev local (mesma regra da
        borda, F1-04). Plugins entram no mesmo registry dos built-ins (``PluginLoadError`` se
        recusados). Síncrono e com I/O: chame fora do event loop.
        """
        config = self.config
        dev_mode = is_local_dev_mode(config)
        registry = ProviderRegistry(
            BUILTIN_PROVIDERS,
            secrets_dir=config.secrets_dir,
            policy=DestinationPolicy(
                allowlist=config.model_base_url_allowlist,
                allow_loopback=dev_mode,
            ),
            operator_base_urls={"ollama": config.ollama_base_url} if config.ollama_base_url else None,
        )
        load_provider_plugins(
            registry,
            allowlist=config.plugin_allowlist,
            logger=self._logger,
            dynamic_specs=config.dynamic_provider_specs,
            dynamic_import_allowed=config.allow_dynamic_import and dev_mode,
        )
        return registry

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
