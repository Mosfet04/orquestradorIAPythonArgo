"""Controller do orquestrador de agentes — agno v2.5."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from agno.agent import Agent
from agno.team import Team

from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase
from src.application.use_cases.get_active_teams_use_case import GetActiveTeamsUseCase
from src.domain.ports import ILogger


class AgentCacheEntry:
    """Cache de agentes com TTL."""

    def __init__(self, agents: List[Agent], ttl_minutes: int = 5) -> None:
        self.agents = agents
        self.created_at = datetime.now(timezone.utc)
        self.ttl = timedelta(minutes=ttl_minutes)
        self.hit_count = 0
        self.last_access = self.created_at

    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) > (self.created_at + self.ttl)

    def access(self) -> List[Agent]:
        self.hit_count += 1
        self.last_access = datetime.now(timezone.utc)
        return self.agents


class TeamCacheEntry:
    """Cache de teams com TTL."""

    def __init__(self, teams: List[Team], ttl_minutes: int = 5) -> None:
        self.teams = teams
        self.created_at = datetime.now(timezone.utc)
        self.ttl = timedelta(minutes=ttl_minutes)
        self.hit_count = 0
        self.last_access = self.created_at

    def is_expired(self) -> bool:
        return datetime.now(timezone.utc) > (self.created_at + self.ttl)

    def access(self) -> List[Team]:
        self.hit_count += 1
        self.last_access = datetime.now(timezone.utc)
        return self.teams


class OrquestradorController:
    """Gerencia o orquestrador de agentes e teams com cache."""

    def __init__(
        self,
        get_active_agents_use_case: GetActiveAgentsUseCase,
        get_active_teams_use_case: GetActiveTeamsUseCase,
        logger: ILogger,
    ) -> None:
        self._agents_use_case = get_active_agents_use_case
        self._teams_use_case = get_active_teams_use_case
        self._logger = logger
        self._cache: Optional[AgentCacheEntry] = None
        self._team_cache: Optional[TeamCacheEntry] = None
        self._lock = asyncio.Lock()

    async def get_agents(self) -> List[Agent]:
        """Retorna agentes com cache inteligente."""
        async with self._lock:
            if self._cache and not self._cache.is_expired():
                return self._cache.access()
        return await self._load_agents()

    async def get_teams(self) -> List[Team]:
        """Retorna teams com cache inteligente."""
        async with self._lock:
            if self._team_cache and not self._team_cache.is_expired():
                return self._team_cache.access()
        # Teams dependem de agents — garante que agents existam
        agents = await self.get_agents()
        return await self._load_teams(agents)

    async def warm_up_cache(self) -> None:
        """Pre-aquece o cache de agentes e teams durante a inicialização."""
        agents = await self.get_agents()
        await self._load_teams(agents)

    async def refresh_agents(self) -> None:
        """Força recarga do cache de agentes e teams."""
        async with self._lock:
            self._cache = None
            self._team_cache = None
        agents = await self._load_agents()
        await self._load_teams(agents)
        self._logger.info("Cache de agentes e teams atualizado")

    def get_cache_stats(self) -> Dict[str, Any]:
        stats: Dict[str, Any] = {}
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

    async def _load_agents(self) -> List[Agent]:
        try:
            agents = await self._agents_use_case.execute()
            self._cache = AgentCacheEntry(agents)
            return agents
        except Exception as exc:
            self._logger.error(
                "Erro ao carregar agentes",
                error=str(exc),
            )
            if self._cache:
                self._logger.warning("Usando cache expirado como fallback")
                return self._cache.access()
            raise

    async def _load_teams(self, agents: List[Agent]) -> List[Team]:
        try:
            teams = await self._teams_use_case.execute(agents)
            self._team_cache = TeamCacheEntry(teams)
            return teams
        except Exception as exc:
            self._logger.error(
                "Erro ao carregar teams",
                error=str(exc),
            )
            if self._team_cache:
                self._logger.warning("Usando cache de teams expirado como fallback")
                return self._team_cache.access()
            return []