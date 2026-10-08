"""Factory para criação da aplicação FastAPI com AgentOS (agno v2.5)."""

from __future__ import annotations

import os
from contextlib import asynccontextmanager
from typing import Optional

from agno.os import AgentOS
from agno.os.interfaces.agui import AGUI
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.dependency_injection import DependencyContainer
from src.infrastructure.logging.logger_adapter import StructlogLoggerAdapter
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
    """Cria e configura a aplicação FastAPI + AgentOS.

    A app FastAPI é criada **sincronamente** para que ``uvicorn`` receba
    um objeto ASGI real (não uma coroutine).  Toda inicialização async
    (DI, agentes, AgentOS) acontece dentro do *lifespan*.
    """

    def __init__(self) -> None:
        self._container: Optional[DependencyContainer] = None
        self._config: Optional[AppConfig] = None
        self._api_keys: Optional[ApiKeys] = None
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

        O ``AgentOS.get_app()`` troca o CORS do base_app por um com métodos e headers ``*``
        e origens mescladas (agno/os/utils.py, ``update_cors_middleware``) e põe o
        ``TrailingSlashMiddleware`` na frente; por isso esta função roda de novo depois da
        montagem, deixando a ordem CORS -> auth -> demais.
        """
        if self._config is None:
            raise RuntimeError("AppConfig não carregado: chame create_app() antes")
        config = self._config
        edge = (CORSMiddleware, ApiKeyAuthMiddleware)
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
            # expose_headers no default (nenhum): o navegador só lê os headers
            # CORS-safelisted da resposta.
        )

    # ── admin endpoints ─────────────────────────────────────────────

    def _add_admin_endpoints(self, app: FastAPI) -> None:
        @app.get("/livez")
        async def liveness():
            """Processo vivo. Sem dependências (Mongo/OTel): é o alvo do HEALTHCHECK."""
            return {"status": "ok"}

        @app.get("/admin/health")
        async def health_check():
            if self._container and self._container.health_service:
                result = await self._container.health_service.check_async()
                status_code = 200 if result.get("status") == "healthy" else 503
                return JSONResponse(result, status_code=status_code)
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
        if self._config is None:
            self._config = AppConfig.load()
        config = self._config
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
        except Exception as exc:
            self._logger.warning(
                "OpenTelemetry FastAPI instrumentation não disponível — ignorando",
                error_type=type(exc).__name__,
            )

    def _mount_agent_os(self, app: FastAPI, agents: list, teams: list) -> None:
        """Cria interfaces AG-UI e monta o AgentOS no app base."""
        agent_interfaces = [AGUI(agent=agent) for agent in agents]
        team_interfaces = [AGUI(team=team) for team in teams]
        interfaces = agent_interfaces + team_interfaces
        self._logger.info(
            "Lifespan: montando AgentOS com interfaces AG-UI",
            interface_count=len(interfaces),
        )
        if self._config is None:
            raise RuntimeError("AppConfig não carregado: chame create_app() antes")
        origins = self._config.cors_allowed_origins
        agent_os = AgentOS(
            agents=agents,
            teams=teams or None,
            interfaces=interfaces,
            cors_allowed_origins=list(origins),
            base_app=app,
            on_route_conflict="preserve_base_app",
            tracing=False,
            telemetry=False,
        )
        try:
            agent_os.get_app()
        finally:
            # get_app() troca o CORS por um com "*" (update_cors_middleware): mesmo se
            # falhar depois disso, o app não pode ficar com ele nem perder a auth.
            self._apply_edge_middleware(app)
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
        import time as _time

        startup_start = _time.perf_counter()
        try:
            self._logger.info("Lifespan: iniciando...")
            await self._ensure_container()

            config = self._container.config
            setup_telemetry(config)

            agents, teams = await self._load_all_entities()
            self._try_mount_agent_os(app, agents, teams)

            self._record_startup_metrics(startup_start, agents, teams)
            yield
        except Exception as exc:
            self._logger.error(
                "Erro crítico no lifespan",
                error_type=exc.__class__.__name__,
                error=str(exc),
            )
            raise
        finally:
            shutdown_telemetry()
            if self._container:
                await self._container.cleanup()

    async def _load_all_entities(self):
        """Carrega agentes e teams ativos."""
        agents = await self._load_agents()
        teams = await self._load_teams()
        return agents, teams

    def _try_mount_agent_os(self, app: FastAPI, agents, teams):
        """Tenta montar AgentOS se houver agentes ou teams."""
        if not agents and not teams:
            self._logger.info(
                "Nenhum agente ou team ativo — rodando só endpoints admin"
            )
            return

        try:
            self._mount_agent_os(app, agents or [], teams or [])
        except Exception as exc:
            self._logger.error(
                "Erro ao montar AgentOS — continuando sem rotas de agente",
                error_type=exc.__class__.__name__,
                error=str(exc),
            )

    def _record_startup_metrics(self, startup_start, agents, teams):
        """Registra métricas de startup."""
        import time as _time
        startup_elapsed = _time.perf_counter() - startup_start
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
