import asyncio
from typing import List, Optional
from datetime import datetime, timedelta
from agno.agent import Agent
from agno.playground import Playground
from agno.app.fastapi import FastAPIApp
from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase
from src.infrastructure.logging import app_logger


class AgentCacheEntry:
    """Entrada otimizada de cache com TTL e métricas."""
    
    def __init__(self, agents: List[Agent], ttl_minutes: int = 5):
        self.agents = agents
        self.created_at = datetime.utcnow()
        self.ttl = timedelta(minutes=ttl_minutes)
        self.hit_count = 0
        self.last_access = self.created_at
    
    def is_expired(self) -> bool:
        """Verifica se o cache expirou."""
        return datetime.utcnow() > (self.created_at + self.ttl)
    
    def access(self) -> List[Agent]:
        """Acessa o cache e atualiza métricas."""
        self.hit_count += 1
        self.last_access = datetime.utcnow()
        return self.agents


class OrquestradorController:
    """Controller responsável por gerenciar o orquestrador de agentes - Otimizado."""
    
    def __init__(self, get_active_agents_use_case: GetActiveAgentsUseCase):
        self._get_active_agents_use_case = get_active_agents_use_case
        self._agents_cache: Optional[AgentCacheEntry] = None
        self._cache_lock = asyncio.Lock()
        self._cache_warming = False
    
    async def get_agents_async(self) -> List[Agent]:
        """Obtém a lista de agentes com cache inteligente assíncrono."""
        async with self._cache_lock:
            # Verificar se cache existe e não expirou
            if self._agents_cache and not self._agents_cache.is_expired():
                app_logger.debug("🎯 Cache hit para agentes", 
                               hit_count=self._agents_cache.hit_count)
                return self._agents_cache.access()
            
            # Cache miss ou expirado - recarregar
            start_time = datetime.utcnow()
            
            try:
                agents = await self._get_active_agents_use_case.execute_async()
                
                # Atualizar cache
                self._agents_cache = AgentCacheEntry(agents)
                
                return agents
                
            except Exception as e:
                app_logger.error("❌ Erro ao carregar agentes", 
                               error=str(e), error_type=e.__class__.__name__)
                
                # Retornar cache antigo se disponível em caso de erro
                if self._agents_cache:
                    app_logger.warning("⚠️ Usando cache expirado devido a erro")
                    return self._agents_cache.access()
                
                raise
    
    def get_agents(self) -> List[Agent]:
        """Versão síncrona mantida para compatibilidade."""
        return asyncio.run(self.get_agents_async())
    
    async def warm_up_cache(self) -> None:
        """Pre-aquece o cache durante a inicialização."""
        if self._cache_warming:
            return
            
        self._cache_warming = True
        try:
            await self.get_agents_async()
        finally:
            self._cache_warming = False
    
    async def create_playground_async(self) -> Playground:
        """Cria playground assincronamente com agentes em cache."""
        agents = await self.get_agents_async()
        
        app_logger.debug("🎮 Criando playground", agent_count=len(agents))
        
        return Playground(
            agents=agents,
            name="Playground Otimizado",
            description="Playground para agentes múltiplos com cache otimizado",
            app_id="playground_optimized",
        )
    
    def create_playground(self) -> Playground:
        """Versão síncrona mantida para compatibilidade."""
        return asyncio.run(self.create_playground_async())
    
    async def create_fastapi_app_async(self) -> FastAPIApp:
        """Cria FastAPI app assincronamente com agentes em cache."""
        agents = await self.get_agents_async()
        
        app_logger.debug("⚡ Criando FastAPI app", agent_count=len(agents))
        
        return FastAPIApp(
            agents=agents,
            name="API Fast Otimizada",
            app_id="api_fast_optimized",
            description="API Fast otimizada para consumo de múltiplos agentes",
        )
    
    def create_fastapi_app(self) -> FastAPIApp:
        """Versão síncrona mantida para compatibilidade."""
        return asyncio.run(self.create_fastapi_app_async())
    
    async def refresh_agents_async(self) -> None:
        """Força atualização do cache de agentes assincronamente."""
        async with self._cache_lock:
            app_logger.info("🔄 Invalidando cache de agentes manualmente")
            self._agents_cache = None
            
            # Recarregar imediatamente
            await self.get_agents_async()
    
    def refresh_agents(self) -> None:
        """Versão síncrona mantida para compatibilidade."""
        asyncio.run(self.refresh_agents_async())
    
    def get_cache_stats(self) -> dict:
        """Retorna estatísticas do cache para monitoramento."""
        if not self._agents_cache:
            return {"status": "empty"}
        
        return {
            "status": "active",
            "hit_count": self._agents_cache.hit_count,
            "created_at": self._agents_cache.created_at.isoformat(),
            "last_access": self._agents_cache.last_access.isoformat(),
            "is_expired": self._agents_cache.is_expired(),
            "agent_count": len(self._agents_cache.agents)
        }