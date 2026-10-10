"""Container de injeção de dependências — Composition Root."""

from __future__ import annotations

import asyncio
from typing import Any

from motor.motor_asyncio import AsyncIOMotorClient

from src.application.services.document_indexing_service import DocumentIndexingService
from src.application.services.knowledge_search_factory import KnowledgeSearchFactory
from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase
from src.application.use_cases.get_active_teams_use_case import GetActiveTeamsUseCase
from src.domain.ports import ILogger
from src.domain.repositories.agent_config_repository import IAgentConfigRepository
from src.domain.repositories.team_config_repository import ITeamConfigRepository
from src.domain.repositories.tool_repository import IToolRepository
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
from src.infrastructure.repositories.yaml_config_repository import (
    YamlAgentConfigRepository,
    YamlConfigFile,
    YamlTeamConfigRepository,
    YamlToolRepository,
)
from src.infrastructure.runtime.agno import AgnoRuntime
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService
from src.infrastructure.runtime.agno.team_factory_service import TeamFactoryService
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
        except Exception as exc:  # noqa: BLE001 - health reporta unhealthy; tipo no log
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
        self._mongo_client: AsyncIOMotorClient | None = None
        self._health_service: HealthService | None = None
        self._controller: OrquestradorController | None = None
        self._runtime: AgnoRuntime | None = None

    @classmethod
    async def create_async(cls, config: AppConfig) -> DependencyContainer:
        """Container pronto; se a inicialização falhar no meio, fecha o que já abriu e re-levanta.

        Quem chama só recebe o container quando tudo deu certo, então não teria como fechar o
        cliente Mongo de uma inicialização parcial (BUG-F2-03-QA-2).
        """
        container = cls(config)
        try:
            await container._initialize()
        except BaseException:
            await container.cleanup()
            raise
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
        except Exception as exc:  # noqa: BLE001 - o app sobe sem Mongo (health reporta); logado
            self._logger.warning(
                "MongoDB não disponível na inicialização", error=str(exc)
            )

        self._health_service = HealthService(self._mongo_client, self._logger)

        # ── Wiring ──────────────────────────────────────────────────
        conn = self.config.mongo_connection_string
        db = self.config.mongo_database_name

        tool_factory = HttpToolFactory(logger=self._logger)

        agent_config_repo, team_config_repo, tool_repo = self._build_config_repositories()

        # ── Hierárquica: parser, tree repo, summary gen, factories ──
        doc_parser = TextDocumentParser()
        tree_repo = MongoDocumentTreeRepository(
            connection_string=conn, database_name=db, logger=self._logger
        )
        try:
            await tree_repo.ensure_indexes()
        except Exception as exc:  # noqa: BLE001 - índices são otimização; logado
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
        runtime = AgnoRuntime(agent_factory=agent_factory, team_factory=team_factory)
        self._runtime = runtime

        agents_use_case = GetActiveAgentsUseCase(
            runtime, agent_config_repo, self._logger
        )
        teams_use_case = GetActiveTeamsUseCase(
            runtime, team_config_repo, self._logger
        )

        self._controller = OrquestradorController(
            get_active_agents_use_case=agents_use_case,
            get_active_teams_use_case=teams_use_case,
            logger=self._logger,
        )

    def _build_config_repositories(
        self,
    ) -> tuple[IAgentConfigRepository, ITeamConfigRepository, IToolRepository]:
        """Config de agentes, teams e tools pelo ``CONFIG_STORE`` (F2-06): Mongo ou o arquivo YAML.

        Com ``yaml``, nenhuma coleção de config do Mongo é usada; o Mongo continua sendo o das
        sessões, da memória e da árvore de documentos do RAG.
        """
        config = self.config
        # O AppConfig garante caminho com "yaml" (__post_init__); o "is not None" só estreita o tipo.
        if config.config_store == "yaml" and config.config_yaml_path is not None:
            source = YamlConfigFile(config.config_yaml_path, logger=self._logger)
            self._logger.info("Config de agentes, teams e tools: arquivo YAML", path=source.path)
            return (
                YamlAgentConfigRepository(source, logger=self._logger),
                YamlTeamConfigRepository(source, logger=self._logger),
                YamlToolRepository(source, logger=self._logger),
            )
        conn, db = config.mongo_connection_string, config.mongo_database_name
        return (
            MongoAgentConfigRepository(connection_string=conn, database_name=db, logger=self._logger),
            MongoTeamConfigRepository(connection_string=conn, database_name=db, logger=self._logger),
            MongoToolRepository(connection_string=conn, database_name=db, logger=self._logger),
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

    def get_agent_runtime(self) -> AgnoRuntime:
        """Runtime que monta as entidades e as serve no app (``mount``/``start``/``close``)."""
        if self._runtime is None:
            raise RuntimeError("Container não inicializado: chame create_async() antes")
        return self._runtime

    @property
    def health_service(self) -> HealthService | None:
        return self._health_service

    async def cleanup(self) -> None:
        if self._mongo_client:
            try:
                result: Any = self._mongo_client.close()
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:  # noqa: BLE001 - shutdown segue; tipo no log
                # Shutdown segue mesmo se o driver falhar ao fechar; o tipo vai ao log.
                self._logger.warning(
                    "Falha ao fechar o cliente MongoDB", error_type=type(exc).__name__
                )
