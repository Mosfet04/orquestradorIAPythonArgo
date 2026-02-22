"""Controller do orquestrador de agentes — agno v2.5."""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from agno.agent import Agent

from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase
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


class OrquestradorController:
    """Gerencia o orquestrador de agentes com cache."""

    def __init__(
        self,
        get_active_agents_use_case: GetActiveAgentsUseCase,
        logger: ILogger,
    ) -> None:
        self._use_case = get_active_agents_use_case
        self._logger = logger
        self._cache: Optional[AgentCacheEntry] = None
        self._lock = asyncio.Lock()

    async def get_agents(self) -> List[Agent]:
        """Retorna agentes com cache inteligente."""
        async with self._lock:
            if self._cache and not self._cache.is_expired():
                return self._cache.access()
        return await self._load_agents()

    async def warm_up_cache(self) -> None:
        """Pre-aquece o cache durante a inicialização."""
        await self.get_agents()

    async def refresh_agents(self) -> None:
        """Força recarga do cache."""
        async with self._lock:
            self._cache = None
        await self._load_agents()
        self._logger.info("Cache de agentes atualizado")

    def get_cache_stats(self) -> Dict[str, Any]:
        if not self._cache:
            return {"status": "empty"}
        return {
            "status": "active",
            "hit_count": self._cache.hit_count,
            "created_at": self._cache.created_at.isoformat(),
            "last_access": self._cache.last_access.isoformat(),
            "is_expired": self._cache.is_expired(),
            "agent_count": len(self._cache.agents),
        }

    # ── private ─────────────────────────────────────────────────────

    async def _load_agents(self) -> List[Agent]:
        try:
            agents = await self._use_case.execute()
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