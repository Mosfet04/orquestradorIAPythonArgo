"""``AgnoRuntime``: implementação da porta ``AgentRuntime`` com o agno 2.5 (F2-04).

Inclui o ciclo de vida do AgentOS no app (F2-05), o db de sessões compartilhado com o AgentOS (F2-08) e a
troca a quente das entidades servidas (FA-04).
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from contextlib import AsyncExitStack, asynccontextmanager
from typing import Any, cast

from agno.agent import Agent
from agno.db.base import AsyncBaseDb, BaseDb
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
from src.infrastructure.runtime.agno.served_entities import ServedEntities
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
    lifespans do AgentOS) -> ``close`` no shutdown. Entre eles, ``swap`` troca as entidades servidas (FA-04).

    ``session_db``: o db de sessões e memória que as fábricas deram às entidades; vai também como
    ``db=`` ao AgentOS. Tem de ser assíncrono (``AsyncBaseDb``): com um ``BaseDb`` síncrono as
    rotas do AgentOS carregam do banco agente/team que não existe no código
    (``agno/os/utils.py``, ``get_agent_by_id``/``get_team_by_id``) e ``/components`` passa a
    gravá-los, fora do ``ProviderRegistry`` e do guardrail de ``user_id``. Quem fecha o db é quem
    o criou (o composition root) e o lifespan de db do AgentOS.
    """

    def __init__(
        self,
        *,
        agent_factory: AgentFactoryService,
        team_factory: TeamFactoryService,
        session_db: AsyncBaseDb | None,
    ) -> None:
        if isinstance(session_db, BaseDb):
            raise TypeError(
                "session_db do AgnoRuntime precisa ser assíncrono (AsyncBaseDb): com um db síncrono o "
                "AgentOS carregaria agentes e teams gravados no banco"
            )
        self._agent_factory = agent_factory
        self._team_factory = team_factory
        self._session_db = session_db
        self._app: FastAPI | None = None
        self._agent_os: AgentOS | None = None
        self._served = ServedEntities(StructlogLoggerAdapter("agui"))
        self._revision = 0
        self._agent_os_lifespan: Lifespan[Any] | None = None
        self._open_lifespans: AsyncExitStack | None = None

    async def build_agent(self, config: AgentConfig) -> AgentHandle:
        """Monta o agente pela fábrica do agno."""
        return cast(AgentHandle, await self._agent_factory.create_agent(config))

    def build_team(self, config: TeamConfig, agents: Sequence[AgentHandle]) -> TeamHandle:
        """Monta o team com os agentes já montados por este runtime."""
        # Os handles de agente deste runtime são ``agno.Agent`` (criados por ``build_agent``).
        members = cast(list[Agent], list(agents))
        return cast(TeamHandle, self._team_factory.create_team(config, members))

    @property
    def revision(self) -> int:
        """Revisão das entidades servidas: 0 = as da montagem; cada ``swap`` soma 1."""
        return self._revision

    def swap(self, agents: Sequence[AgentHandle], teams: Sequence[TeamHandle]) -> int:
        """Passa a servir ``agents`` e ``teams`` no AgentOS, no AG-UI e no cancel; devolve a revisão nova.

        Sem ``await``: nenhum request intercala a troca, que é atômica para quem lê. Request que já pegou a
        entidade (run em andamento, em stream inclusive) termina com a instância antiga; as rotas do AgentOS
        ainda copiam a entidade a cada run (``create_fresh``). Nada de ``AgentOS.resync`` (interno, muda na 3.x):
        as rotas do AgentOS leem ``os.agents``/``os.teams`` a cada request (``get_agent_by_id``,
        ``agno/os/routers/agents/router.py:457`` no 2.5.8). Falha no preparo sobe antes de trocar qualquer coisa.
        """
        agno_agents = cast(list[Agent], list(agents))
        agno_teams = cast(list[Team], list(teams))
        if self._agent_os is not None:
            self._serve_in_agent_os(self._agent_os, agno_agents, agno_teams)
        self._served.replace(agno_agents, agno_teams)
        self._revision += 1
        return self._revision

    def _serve_in_agent_os(self, agent_os: AgentOS, agents: list[Agent], teams: list[Team]) -> None:
        """Prepara as entidades como o AgentOS do app preparou as do startup e as põe nas listas dele.

        O preparo (db padrão, ``initialize_agent``/``initialize_team``, ``store_events``, ids repetidos) é o
        do construtor do AgentOS, a API pública que o faz: um AgentOS sem app, descartado. As listas são as
        mesmas, com conteúdo novo: as rotas que as guardaram na montagem (``/eval-runs``) e o ``/registry``
        veem as entidades novas.
        """
        served_agents, served_teams = agent_os.agents, agent_os.teams
        if served_agents is None or served_teams is None:
            raise RuntimeError("AgentOS montado sem a lista de entidades")
        if agents or teams:
            self._build_agent_os(None, agents, teams, ())
        served_agents[:] = agents
        served_teams[:] = teams
        if agent_os.registry is not None:
            agent_os.registry.agents[:] = agents
            agent_os.registry.teams[:] = teams

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
        agent_os = self._build_agent_os(app, agno_agents, agno_teams, cors_allowed_origins)
        self._served.replace(agno_agents, agno_teams)
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
            app.include_router(build_run_cancel_router(self._served, StructlogLoggerAdapter("run_cancel")))
            agent_os.get_app()
            app.include_router(build_agui_router(self._served, StructlogLoggerAdapter("agui")))
            agent_os_lifespan = app.router.lifespan_context
        finally:
            app.router.lifespan_context = own_lifespan
        app.openapi_schema = None
        self._app = app
        self._agent_os = agent_os
        self._agent_os_lifespan = agent_os_lifespan

    def _build_agent_os(
        self,
        app: FastAPI | None,
        agno_agents: list[Agent],
        agno_teams: list[Team],
        cors_allowed_origins: Sequence[str],
    ) -> AgentOS:
        """AgentOS sobre o ``app``, sem telemetria nem tracing e com o db de sessões compartilhado.

        Sem ``app``, só prepara as entidades (o construtor não toca rota nem middleware): o preparo da troca.
        """
        return AgentOS(
            # O db das entidades: habilita /schedules e /approvals (admin na auth da borda);
            # /components segue stub 503 e nada é carregado do banco, porque o db é assíncrono.
            db=self._session_db,
            agents=list(agno_agents),  # cópia: o AgentOS tipa list[Agent | RemoteAgent] (invariante)
            # Lista mesmo vazia: a troca a quente (swap) a reenche no lugar.
            teams=list(agno_teams),
            cors_allowed_origins=list(cors_allowed_origins),
            base_app=app,
            on_route_conflict="preserve_base_app",
            tracing=False,
            telemetry=False,
            # Sem provisionar coleções no startup: o db_lifespan aguardaria _create_all_tables
            # (com o Mongo fora, cada coleção espera a seleção de servidor e o startup atrasa
            # minutos). O agno cria a coleção e os índices na 1ª operação, como sempre foi.
            auto_provision_dbs=False,
        )

    async def start(self) -> None:
        """Abre os lifespans do AgentOS montado (nada a fazer sem ``mount``).

        Falha sobe (o startup do app falha, como no AgentOS) e ``close`` continua seguro. Os
        lifespans do agno 2.5.8 não têm ``try/finally``: os que já tinham aberto não rodam o
        fechamento (o db de sessões é fechado por quem o criou, o ``cleanup`` do container).
        """
        if self._app is None or self._agent_os_lifespan is None or self._open_lifespans is not None:
            return
        stack = AsyncExitStack()
        await stack.enter_async_context(self._agent_os_lifespan(self._app))
        self._open_lifespans = stack

    async def close(self) -> None:
        """Fecha os lifespans abertos por ``start`` (db de sessões, clientes httpx do agno).

        Idempotente: a segunda chamada, ou sem ``start``, não faz nada.
        """
        stack, self._open_lifespans = self._open_lifespans, None
        if stack is not None:
            await stack.aclose()
