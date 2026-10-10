"""``AgnoRuntime``: implementação da porta ``AgentRuntime`` com o agno 2.5 (F2-04) e ciclo de vida
do AgentOS no app (F2-05)."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, cast

from agno.agent import Agent
from agno.os import AgentOS
from agno.team import Team
from fastapi import FastAPI
from starlette.types import Lifespan

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.team_config import TeamConfig
from src.domain.ports import AgentHandle, AgentRuntime, TeamHandle
from src.infrastructure.logging.logger_adapter import StructlogLoggerAdapter
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService
from src.infrastructure.runtime.agno.agui_router import build_agui_router
from src.infrastructure.runtime.agno.run_cancellation import (
    build_run_cancel_router,
    install_run_cancellation_manager,
)
from src.infrastructure.runtime.agno.team_factory_service import TeamFactoryService


@asynccontextmanager
async def _no_lifespan(_app: object) -> AsyncIterator[None]:
    """Lifespan vazio no lugar do nosso enquanto o ``get_app()`` combina os do AgentOS."""
    yield


class AgnoRuntime(AgentRuntime):
    """Monta ``agno.Agent``/``agno.Team`` pelas fábricas e os serve num ``AgentOS`` sobre o app.

    Os handles são os próprios objetos do agno: por isso ``mount`` os usa direto.
    ``Agent.id``/``Team.id`` são ``Optional[str]`` no agno; aqui são sempre o id da configuração
    (texto validado no domínio).

    Ciclo de vida no lifespan do app: ``mount`` (rotas, síncrono e sem I/O) -> ``start`` (abre os
    lifespans do AgentOS) -> ``close`` no shutdown.
    """

    def __init__(self, *, agent_factory: AgentFactoryService, team_factory: TeamFactoryService) -> None:
        self._agent_factory = agent_factory
        self._team_factory = team_factory
        self._app: FastAPI | None = None
        self._agent_os_lifespan: Lifespan[Any] | None = None
        self._open_lifespans: AsyncExitStack | None = None

    async def build_agent(self, config: AgentConfig) -> AgentHandle:
        return cast(AgentHandle, await self._agent_factory.create_agent(config))

    def build_team(self, config: TeamConfig, agents: Sequence[AgentHandle]) -> TeamHandle:
        # Os handles de agente deste runtime são ``agno.Agent`` (criados por ``build_agent``).
        members = cast(list[Agent], list(agents))
        return cast(TeamHandle, self._team_factory.create_team(config, members))

    def mount(
        self,
        app: FastAPI,
        agents: Sequence[AgentHandle],
        teams: Sequence[TeamHandle],
        *,
        cors_allowed_origins: Sequence[str],
    ) -> None:
        """Monta no ``app`` o AgentOS, as rotas de cancel (F1-10) e, depois dele, o AG-UI por entidade.

        Sem a interface ``AGUI`` do agno: ela só expõe uma entidade em ``/agui`` e fixa
        ``Access-Control-Allow-Origin: *`` (ver ``agui_router``). O router entra depois do
        ``get_app()``, quando o AgentOS já definiu os ids das entidades.

        O ``get_app()`` troca o CORS do app por um com ``*`` (``update_cors_middleware``, em
        ``agno/os/utils.py``) e põe o ``TrailingSlashMiddleware`` na frente dos demais: quem monta
        reinstala a borda (CORS e auth) por fora logo depois, mesmo se esta função falhar.

        Os lifespans do AgentOS (db, clientes httpx, MCP) ficam guardados para ``start``: o
        ``get_app()`` só os combina com o lifespan do app (``agno/os/app.py``, ``get_app``), que
        aqui já está rodando; o do app continua o nosso.
        """
        if self._app is not None:
            raise RuntimeError("AgentOS já montado por este runtime")
        agno_agents = cast(list[Agent], list(agents))
        agno_teams = cast(list[Team], list(teams))
        agent_os = AgentOS(
            agents=list(agno_agents),  # cópia: o AgentOS tipa list[Agent | RemoteAgent] (invariante)
            teams=list(agno_teams) or None,
            cors_allowed_origins=list(cors_allowed_origins),
            base_app=app,
            on_route_conflict="preserve_base_app",
            tracing=False,
            telemetry=False,
            # Sem provisionar coleções no startup: o db de sessões é o MongoDb síncrono e o
            # db_lifespan chamaria _create_all_tables no event loop para cada entidade (com o
            # Mongo fora, cada um espera o server selection). O agno cria a coleção na 1ª escrita.
            auto_provision_dbs=False,
        )
        # Cancel só de run registrado (F1-10): gerenciador antes de qualquer run e as rotas
        # de cancel antes do get_app(), que pula as do agno (preserve_base_app). Depois do
        # construtor (que valida ids): falha ali não deixa rota parcial.
        install_run_cancellation_manager()
        # Enquanto monta, o lifespan do app é um vazio: o get_app() combina com ele os do AgentOS
        # e cada include_router funde o do router (FastAPI); o combinado fica para start() e o
        # do app volta a ser exatamente o nosso (sem rodar o nosso de novo dentro dele).
        own_lifespan = app.router.lifespan_context
        app.router.lifespan_context = _no_lifespan
        try:
            app.include_router(
                build_run_cancel_router(agno_agents, agno_teams, StructlogLoggerAdapter("run_cancel"))
            )
            agent_os.get_app()
            app.include_router(build_agui_router(agno_agents, agno_teams, StructlogLoggerAdapter("agui")))
            agent_os_lifespan = app.router.lifespan_context
        finally:
            app.router.lifespan_context = own_lifespan
        app.openapi_schema = None
        self._app = app
        self._agent_os_lifespan = agent_os_lifespan

    async def start(self) -> None:
        """Abre os lifespans do AgentOS montado (nada a fazer sem ``mount``).

        Falha sobe (o startup do app falha, como no AgentOS) e ``close`` continua seguro. Os
        lifespans do agno 2.5.8 não têm ``try/finally``: os que já tinham aberto não rodam o
        fechamento (ex.: dbs das entidades ficam para o fim do processo, que sai com o startup
        recusado).
        """
        if self._app is None or self._agent_os_lifespan is None or self._open_lifespans is not None:
            return
        stack = AsyncExitStack()
        await stack.enter_async_context(self._agent_os_lifespan(self._app))
        self._open_lifespans = stack

    async def close(self) -> None:
        """Fecha os lifespans abertos por ``start`` (dbs das entidades, clientes httpx do agno).

        Idempotente: a segunda chamada, ou sem ``start``, não faz nada.
        """
        stack, self._open_lifespans = self._open_lifespans, None
        if stack is not None:
            await stack.aclose()
