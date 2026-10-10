"""Controller do orquestrador de agentes: cache dos agentes e teams montados pelo runtime."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from typing import Any

from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase
from src.application.use_cases.get_active_teams_use_case import GetActiveTeamsUseCase
from src.domain.ports import AgentHandle, ILogger, TeamHandle

CacheMetric = Callable[[str], None]
"""Recebe o nome do cache (``"agents"`` ou ``"teams"``) a cada hit ou miss."""


def _no_metric(_cache_name: str) -> None:
    """Sem callback de métrica, hit e miss não são registrados."""


class AgentCacheEntry:
    """Cache de agentes com TTL."""

    def __init__(self, agents: list[AgentHandle], ttl_minutes: int = 5) -> None:
        self.agents = agents
        self.created_at = datetime.now(UTC)
        self.ttl = timedelta(minutes=ttl_minutes)
        self.hit_count = 0
        self.last_access = self.created_at

    def is_expired(self) -> bool:
        return datetime.now(UTC) > (self.created_at + self.ttl)

    def access(self) -> list[AgentHandle]:
        self.hit_count += 1
        self.last_access = datetime.now(UTC)
        return self.agents


class TeamCacheEntry:
    """Cache de teams com TTL."""

    def __init__(self, teams: list[TeamHandle], ttl_minutes: int = 5) -> None:
        self.teams = teams
        self.created_at = datetime.now(UTC)
        self.ttl = timedelta(minutes=ttl_minutes)
        self.hit_count = 0
        self.last_access = self.created_at

    def is_expired(self) -> bool:
        return datetime.now(UTC) > (self.created_at + self.ttl)

    def access(self) -> list[TeamHandle]:
        self.hit_count += 1
        self.last_access = datetime.now(UTC)
        return self.teams


class OrquestradorController:
    """Gerencia o orquestrador de agentes e teams com cache.

    ``on_cache_hit``/``on_cache_miss`` são chamados com o nome do cache; o composition root liga os
    dois às métricas (o controller não importa a telemetria: regra de dependência, F2-07).
    """

    def __init__(
        self,
        get_active_agents_use_case: GetActiveAgentsUseCase,
        get_active_teams_use_case: GetActiveTeamsUseCase,
        logger: ILogger,
        *,
        on_cache_hit: CacheMetric = _no_metric,
        on_cache_miss: CacheMetric = _no_metric,
    ) -> None:
        self._agents_use_case = get_active_agents_use_case
        self._teams_use_case = get_active_teams_use_case
        self._logger = logger
        self._on_cache_hit = on_cache_hit
        self._on_cache_miss = on_cache_miss
        self._cache: AgentCacheEntry | None = None
        self._team_cache: TeamCacheEntry | None = None
        self._lock = asyncio.Lock()

    async def get_agents(self) -> list[AgentHandle]:
        """Retorna agentes com cache inteligente."""
        async with self._lock:
            if self._cache and not self._cache.is_expired():
                self._on_cache_hit("agents")
                return self._cache.access()
        self._on_cache_miss("agents")
        return await self._load_agents()

    async def get_teams(self) -> list[TeamHandle]:
        """Retorna teams com cache inteligente."""
        async with self._lock:
            if self._team_cache and not self._team_cache.is_expired():
                self._on_cache_hit("teams")
                return self._team_cache.access()
        self._on_cache_miss("teams")
        # Teams dependem de agents — garante que agents existam
        agents = await self.get_agents()
        return await self._load_teams(agents)

    async def warm_up_cache(self) -> None:
        """Pre-aquece o cache de agentes e teams durante a inicialização."""
        agents = await self.get_agents()
        await self._load_teams(agents)

    async def refresh_agents(self) -> None:
        """Recarrega agentes e teams e só então troca os dois caches.

        Falha ao recarregar (repositório fora, arquivo de config quebrado...) mantém o cache anterior,
        loga só o tipo do erro e re-levanta (o ``/admin/refresh-cache`` responde erro).
        """
        try:
            agents = await self._agents_use_case.execute()
            teams = await self._teams_use_case.execute(agents)
        except Exception as exc:
            self._logger.error(
                "Falha ao atualizar o cache de agentes e teams; mantido o anterior",
                error_type=type(exc).__name__,
            )
            raise
        async with self._lock:
            self._cache = AgentCacheEntry(agents)
            self._team_cache = TeamCacheEntry(teams)
        self._logger.info("Cache de agentes e teams atualizado")

    def get_cache_stats(self) -> dict[str, Any]:
        stats: dict[str, Any] = {}
        if not self._cache:
            stats["agents"] = {"status": "empty"}
        else:
            stats["agents"] = {
                "status": "active",
                "hit_count": self._cache.hit_count,
                "created_at": self._cache.created_at.isoformat(),
                "last_access": self._cache.last_access.isoformat(),
                "is_expired": self._cache.is_expired(),
                "agent_count": len(self._cache.agents),
            }
        if not self._team_cache:
            stats["teams"] = {"status": "empty"}
        else:
            stats["teams"] = {
                "status": "active",
                "hit_count": self._team_cache.hit_count,
                "created_at": self._team_cache.created_at.isoformat(),
                "last_access": self._team_cache.last_access.isoformat(),
                "is_expired": self._team_cache.is_expired(),
                "team_count": len(self._team_cache.teams),
            }
        return stats

    # ── private ─────────────────────────────────────────────────────

    async def _load_agents(self) -> list[AgentHandle]:
        try:
            agents = await self._agents_use_case.execute()
            self._cache = AgentCacheEntry(agents)
            return agents
        except Exception as exc:
            self._logger.error(
                "Erro ao carregar agentes",
                error_type=type(exc).__name__,
            )
            if self._cache:
                self._logger.warning("Usando cache expirado como fallback")
                return self._cache.access()
            raise

    async def _load_teams(self, agents: list[AgentHandle]) -> list[TeamHandle]:
        try:
            teams = await self._teams_use_case.execute(agents)
            self._team_cache = TeamCacheEntry(teams)
            return teams
        except Exception as exc:  # noqa: BLE001 - logado; teams caem no cache expirado ou ficam vazios
            self._logger.error(
                "Erro ao carregar teams",
                error_type=type(exc).__name__,
            )
            if self._team_cache:
                self._logger.warning("Usando cache de teams expirado como fallback")
                return self._team_cache.access()
            return []
