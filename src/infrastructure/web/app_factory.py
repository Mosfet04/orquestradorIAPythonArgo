"""Borda HTTP da aplicação: FastAPI, CORS, auth, métricas, rotas próprias e lifespan.

O runtime de agentes (hoje o ``AgnoRuntime``, com o AgentOS do agno 2.5) é montado no lifespan:
container -> entidades -> ``runtime.mount``/``start`` e, no shutdown, ``runtime.close``.
"""

from __future__ import annotations

import os
import time
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.domain.ports import AgentHandle, TeamHandle
from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.dependency_injection import DependencyContainer
from src.infrastructure.logging.logger_adapter import StructlogLoggerAdapter
from src.infrastructure.runtime.agno import AgnoRuntime
from src.infrastructure.telemetry import (
    TelemetryMetrics,
    setup_telemetry,
    shutdown_telemetry,
)
from src.infrastructure.web.api_key_auth import (
    ApiKeyAuthMiddleware,
    ApiKeys,
    resolve_api_keys,
)
from src.infrastructure.web.metrics_middleware import MetricsMiddleware

# CORS explícito (nada de "*"): o que o AgentOS/os.agno.com e o AG-UI usam. O Starlette
# soma os headers CORS-safelisted (Accept, Accept-Language, Content-Language, Content-Type).
_CORS_ALLOW_METHODS = ("GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS")
_CORS_ALLOW_HEADERS = ("Authorization", "Content-Type", "X-API-Key")
_CORS_EXPOSE_HEADERS = ("Deprecation", "Link")

# Rotas fora do tracing. O OTel faz re.search em ``scheme://<Host><path>`` (sem query;
# opentelemetry/instrumentation/asgi/__init__.py, get_host_port_url_tuple): a regex
# ancora o path inteiro logo após o host, senão um ``Host: livez`` apagaria o span de
# qualquer rota.
_OTEL_EXCLUDED_URLS = ",".join(
    rf"^[a-z]+://[^/]+{path}$" for path in ("/admin/health", "/livez", "/metrics/cache")
)


def _disable_agno_telemetry_by_default() -> None:
    """Telemetria do Agno desligada por padrão; valor explícito do usuário prevalece.

    O Agno lê ``AGNO_TELEMETRY`` (agno/agent/_init.py, agno/team/_init.py) e, quando ele
    existe, sobrepõe o kwarg ``telemetry`` de Agent/Team.
    """
    os.environ.setdefault("AGNO_TELEMETRY", "false")


class AppFactory:
    """Cria e configura a aplicação FastAPI (borda) e, no lifespan, o runtime de agentes.

    A app FastAPI é criada **sincronamente** para que ``uvicorn`` receba
    um objeto ASGI real (não uma coroutine).  Toda inicialização async
    (DI, agentes, AgentOS) acontece dentro do *lifespan*.
    """

    def __init__(self) -> None:
        self._container: DependencyContainer | None = None
        self._config: AppConfig | None = None
        self._api_keys: ApiKeys | None = None
        self._lifespan_ran = False
        self._logger = StructlogLoggerAdapter("app_factory")

    def create_app(self) -> FastAPI:
        """Cria a aplicação FastAPI — **síncrono** (module-level safe).

        Lê o ``AppConfig`` aqui (configuração inválida falha no startup, antes do bind).
        Sem ``API_KEY_RUN``/``API_KEY_ADMIN`` fora do modo dev local, recusa (fail-closed).
        """
        _disable_agno_telemetry_by_default()
        self._config = AppConfig.load()
        self._api_keys = resolve_api_keys(self._config)
        if self._api_keys is None:
            self._logger.warning(
                "Autenticação desligada: modo dev local sem API_KEY_RUN/API_KEY_ADMIN; só "
                "requests de loopback (cliente, servidor e Host) são atendidos. Proxy ou túnel "
                "no próprio host ainda expõe o app: defina as chaves antes de expor.",
                app_host=self._config.app_host,
                environment=self._config.environment,
            )
        docs = self._config.enable_docs
        base_app = FastAPI(
            title="Orquestrador de Agentes IA",
            description="Sistema de orquestração de agentes IA",
            version="2.0.0",
            lifespan=self._lifespan,
            # Com base_app, o AgentOS não cria rotas de docs: valem só estas.
            docs_url="/docs" if docs else None,
            redoc_url="/redoc" if docs else None,
            openapi_url="/openapi.json" if docs else None,
        )

        self._add_metrics_middleware(base_app)
        # Antes do primeiro evento ASGI (o lifespan inclusive), que monta a pilha de
        # middleware; instrument_app depois disso não gera span nenhum.
        self._instrument_fastapi(base_app)
        self._add_admin_endpoints(base_app)
        # Por último: add_middleware insere na frente, então o CORS fica o mais externo
        # (preflight e headers CORS valem também para respostas de erro dos demais) e a
        # auth logo dentro dele.
        self._apply_edge_middleware(base_app)
        return base_app

    # ── middleware ───────────────────────────────────────────────────

    @staticmethod
    def _add_metrics_middleware(app: FastAPI) -> None:
        """Adiciona middleware de métricas de negócio (agents/teams)."""
        app.add_middleware(MetricsMiddleware)

    def _apply_edge_middleware(self, app: FastAPI) -> None:
        """(Re)instala CORS (mais externo) e, logo dentro dele, a auth por chave de API.

        A montagem do runtime troca o CORS do app por um com métodos e headers ``*`` e põe o
        ``TrailingSlashMiddleware`` na frente (``AgnoRuntime.mount``); por isso esta função roda
        de novo depois da montagem, deixando a ordem CORS -> auth -> demais.
        """
        if self._config is None:
            raise RuntimeError("AppConfig não carregado: chame create_app() antes")
        config = self._config
        edge: tuple[object, ...] = (CORSMiddleware, ApiKeyAuthMiddleware)
        app.user_middleware = [m for m in app.user_middleware if m.cls not in edge]
        app.middleware_stack = None  # reconstruída no próximo request
        app.add_middleware(
            ApiKeyAuthMiddleware,
            keys=self._api_keys,
            docs_require_admin=config.environment != "development",
            allowed_origins=config.cors_allowed_origins,
        )
        app.add_middleware(
            CORSMiddleware,
            allow_origins=list(config.cors_allowed_origins),
            allow_credentials=True,
            allow_methods=list(_CORS_ALLOW_METHODS),
            allow_headers=list(_CORS_ALLOW_HEADERS),
            # O navegador só lê os headers CORS-safelisted e estes: o sinal de alias
            # deprecated do POST /agui (F1-08).
            expose_headers=list(_CORS_EXPOSE_HEADERS),
        )

    # ── admin endpoints ─────────────────────────────────────────────

    def _add_admin_endpoints(self, app: FastAPI) -> None:
        @app.get("/livez")
        async def liveness() -> dict[str, str]:
            """Processo vivo. Sem dependências (Mongo/OTel): é o alvo do HEALTHCHECK."""
            return {"status": "ok"}

        @app.get("/admin/health")
        async def health_check() -> Any:
            if self._container and self._container.health_service:
                result = await self._container.health_service.check_async()
                status_code = 200 if result.get("status") == "healthy" else 503
                return JSONResponse(result, status_code=status_code)
            return {"status": "healthy"}

        @app.get("/metrics/cache")
        async def cache_metrics() -> dict[str, Any]:
            if self._container:
                ctrl = self._container.get_orquestrador_controller()
                return ctrl.get_cache_stats()
            return {"status": "no_cache"}

        @app.post("/admin/refresh-cache")
        async def refresh_cache() -> dict[str, str]:
            if self._container:
                ctrl = self._container.get_orquestrador_controller()
                await ctrl.refresh_agents()
                return {"status": "cache_refreshed"}
            return {"status": "no_cache"}

    # ── lifespan ────────────────────────────────────────────────────

    async def _ensure_container(self) -> DependencyContainer:
        """Garante que o DependencyContainer esteja inicializado."""
        if self._container:
            return self._container
        self._logger.info("Lifespan: carregando AppConfig...")
        if self._config is None:
            self._config = AppConfig.load()
        config = self._config
        self._logger.info(
            "Lifespan: criando DependencyContainer...",
            mongo_db=config.mongo_database_name,
        )
        self._container = await DependencyContainer.create_async(config)
        self._logger.info("Lifespan: container criado com sucesso")
        return self._container

    async def _load_agents(self, container: DependencyContainer) -> list[AgentHandle]:
        """Aquece cache e retorna lista de agentes ativos."""
        controller = container.get_orquestrador_controller()
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

    async def _load_teams(self, container: DependencyContainer) -> list[TeamHandle]:
        """Carrega teams ativos (dependem dos agentes em cache)."""
        teams = await container.get_orquestrador_controller().get_teams()
        self._logger.info(
            "Lifespan: teams carregados",
            team_count=len(teams) if teams else 0,
            team_ids=[t.id for t in teams] if teams else [],
        )
        return teams

    def _instrument_fastapi(self, app: FastAPI) -> None:
        """Aplica auto-instrumentação OpenTelemetry no FastAPI (só nesta app, nunca global).

        O tracer é o proxy global: passa a exportar quando ``setup_telemetry`` define o
        TracerProvider no lifespan; com OTel desligado, os spans são no-op.
        """
        try:
            from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

            FastAPIInstrumentor.instrument_app(
                app,
                excluded_urls=_OTEL_EXCLUDED_URLS,
            )
        except Exception as exc:  # noqa: BLE001 - o app sobe sem tracing HTTP; logado
            self._logger.warning(
                "OpenTelemetry FastAPI instrumentation não disponível — ignorando",
                error_type=type(exc).__name__,
            )

    def _mount_runtime(
        self,
        app: FastAPI,
        runtime: AgnoRuntime,
        agents: Sequence[AgentHandle],
        teams: Sequence[TeamHandle],
    ) -> None:
        """Monta as rotas do runtime (AgentOS, cancel, AG-UI) e reinstala a borda por fora.

        A montagem troca o CORS do app e põe middleware na frente (ver ``AgnoRuntime.mount``):
        a borda volta a ser a mais externa mesmo se a montagem falhar no meio.
        """
        if self._config is None:
            raise RuntimeError("AppConfig não carregado: chame create_app() antes")
        self._logger.info(
            "Lifespan: montando AgentOS",
            agent_count=len(agents),
            team_count=len(teams),
        )
        try:
            runtime.mount(app, agents, teams, cors_allowed_origins=self._config.cors_allowed_origins)
        finally:
            self._apply_edge_middleware(app)
        self._logger.info(
            "AgentOS montado com sucesso",
            agent_count=len(agents),
            team_count=len(teams),
            total_routes=len(app.routes),
        )

    @asynccontextmanager
    async def _lifespan(self, app: FastAPI) -> AsyncIterator[None]:
        """Container -> telemetria -> entidades -> runtime (rotas e lifespans do AgentOS).

        Shutdown, também depois de falha parcial no startup: flush da telemetria primeiro (o fechamento
        do runtime pode demorar e o orquestrador matar o processo), depois o runtime (uma vez) e o
        container. Falha dentro de ``DependencyContainer.create_async`` é fechada por ele mesmo (o
        container só existe aqui quando a inicialização terminou).

        Um lifespan só por ``AppFactory``: o shutdown fecha os clientes do container e os dbs das
        entidades, e as rotas montadas apontam para elas; um 2º lifespan as serviria com o db
        fechado (sessão perdida em silêncio, BUG-F2-05-QA-1). Ele é recusado com erro claro.
        """
        if self._lifespan_ran:
            raise RuntimeError(
                "Lifespan já executado por este AppFactory: o shutdown fechou o container e o runtime; "
                "crie um AppFactory novo (create_app) para subir de novo"
            )
        self._lifespan_ran = True
        startup_start = time.perf_counter()
        runtime: AgnoRuntime | None = None
        try:
            self._logger.info("Lifespan: iniciando...")
            container = await self._ensure_container()
            setup_telemetry(container.config)

            agents, teams = await self._load_all_entities(container)
            runtime = container.get_agent_runtime()
            self._try_mount_runtime(app, runtime, agents, teams)
            await runtime.start()

            self._record_startup_metrics(startup_start, agents, teams)
            yield
        except Exception as exc:
            self._logger.error(
                "Erro crítico no lifespan",
                error_type=exc.__class__.__name__,
            )
            raise
        finally:
            try:
                shutdown_telemetry()
            finally:
                try:
                    if runtime is not None:
                        await runtime.close()
                finally:
                    if self._container:
                        await self._container.cleanup()

    async def _load_all_entities(
        self, container: DependencyContainer
    ) -> tuple[list[AgentHandle], list[TeamHandle]]:
        """Carrega agentes e teams ativos."""
        agents = await self._load_agents(container)
        teams = await self._load_teams(container)
        return agents, teams

    def _try_mount_runtime(
        self,
        app: FastAPI,
        runtime: AgnoRuntime,
        agents: Sequence[AgentHandle],
        teams: Sequence[TeamHandle],
    ) -> None:
        """Monta o runtime no app se houver agentes ou teams; falha deixa a borda, o admin e o que a
        montagem já tiver posto (falha depois do ``get_app()`` deixa as rotas do AgentOS sem os
        lifespans dele)."""
        if not agents and not teams:
            self._logger.info(
                "Nenhum agente ou team ativo — rodando só endpoints admin"
            )
            return

        try:
            self._mount_runtime(app, runtime, agents, teams)
        except Exception as exc:  # noqa: BLE001 - o app sobe sem rotas de agente; logado
            self._logger.error(
                "Erro ao montar AgentOS — continuando com montagem parcial ou sem rotas de agente",
                error_type=exc.__class__.__name__,
            )

    def _record_startup_metrics(
        self, startup_start: float, agents: Sequence[AgentHandle], teams: Sequence[TeamHandle]
    ) -> None:
        """Registra métricas de startup."""
        startup_elapsed = time.perf_counter() - startup_start
        TelemetryMetrics.record_startup_duration(startup_elapsed)
        TelemetryMetrics.record_agents_loaded(len(agents) if agents else 0)
        TelemetryMetrics.record_teams_loaded(len(teams) if teams else 0)
        self._logger.info(
            "Startup completo",
            startup_duration_s=round(startup_elapsed, 3),
        )


# ── module-level factory ────────────────────────────────────────────

_factory = AppFactory()


def create_app() -> FastAPI:
    """Factory **síncrona** global — segura para ``app = create_app()``."""
    return _factory.create_app()
