"""
Serviço de cache para modelos de IA na camada de Infrastructure.

Este serviço implementa um cache otimizado com TTL para modelos de IA,
seguindo os princípios da Onion Architecture.
"""
import asyncio
from datetime import datetime, timedelta
from typing import Dict, Optional, Any, Callable
from src.infrastructure.logging import app_logger


class ModelCacheEntry:
    """Entrada de cache para modelos com TTL e métricas."""
    
    def __init__(self, model: Any, ttl_minutes: int = 30):
        self.model = model
        self.created_at = datetime.utcnow()
        self.ttl = timedelta(minutes=ttl_minutes)
        self.hit_count = 0
        self.last_access = self.created_at
    
    def is_expired(self) -> bool:
        """Verifica se a entrada expirou."""
        return datetime.utcnow() > (self.created_at + self.ttl)
    
    def access(self) -> Any:
        """Acessa o modelo e atualiza métricas."""
        self.hit_count += 1
        self.last_access = datetime.utcnow()
        return self.model


class ModelCacheService:
    """Serviço de cache otimizado para modelos de IA."""
    
    def __init__(self, ttl_minutes: int = 30):
        self._cache: Dict[str, ModelCacheEntry] = {}
        self._cache_lock: Optional[asyncio.Lock] = None
        self._ttl_minutes = ttl_minutes
        self._total_hits = 0
        self._total_misses = 0
    
    def _get_cache_lock(self) -> asyncio.Lock:
        """Obtém o lock de cache, criando-o se necessário."""
        if self._cache_lock is None:
            self._cache_lock = asyncio.Lock()
        return self._cache_lock
    
    async def get_or_create(
        self, 
        cache_key: str, 
        factory_func: Callable, 
        *args, 
        **kwargs
    ) -> Any:
        """
        Obtém modelo do cache ou cria novo usando factory function.
        
        Args:
            cache_key: Chave única para o cache
            factory_func: Função para criar o modelo em caso de cache miss
            *args, **kwargs: Argumentos para a factory function
        """
        async with self._get_cache_lock():
            # Verificar cache hit
            if cache_key in self._cache:
                entry = self._cache[cache_key]
                if not entry.is_expired():
                    self._total_hits += 1
                    return entry.access()
                else:
                    # Cache expirado - remover
                    del self._cache[cache_key]
            
            # Cache miss - criar novo modelo
            self._total_misses += 1
            
            try:
                # Se a factory for async, aguardar diretamente; caso contrário, offload para thread.
                # Obs.: para operações CPU-bound considere usar ProcessPoolExecutor e/ou tornar a factory assíncrona.
                if asyncio.iscoroutinefunction(factory_func):
                    model = await factory_func(*args, **kwargs)
                else:
                    # asyncio.to_thread preserva kwargs corretamente e usa o ThreadPool padrão
                    model = await asyncio.to_thread(factory_func, *args, **kwargs)
                
                # Adicionar ao cache
                self._cache[cache_key] = ModelCacheEntry(model, self._ttl_minutes)
                
                return model
                
            except Exception as e:
                app_logger.error("❌ Erro ao criar modelo", 
                               cache_key=cache_key, error=str(e))
                raise
    
    async def invalidate(self, cache_key: Optional[str] = None) -> None:
        """
        Invalida cache específico ou todo o cache.
        
        Args:
            cache_key: Chave específica para invalidar ou None para invalidar tudo
        """
        async with self._get_cache_lock():
            if cache_key:
                if cache_key in self._cache:
                    del self._cache[cache_key]
            else:
                self._cache.clear()
    
    async def cleanup_expired(self) -> int:
        """Remove entradas expiradas do cache."""
        async with self._get_cache_lock():
            expired_keys = [
                key for key, entry in self._cache.items() 
                if entry.is_expired()
            ]
            
            for key in expired_keys:
                del self._cache[key]
            
            return len(expired_keys)
    
    def get_stats(self) -> dict:
        """Retorna estatísticas do cache."""
        total_requests = self._total_hits + self._total_misses
        hit_rate = (self._total_hits / total_requests * 100) if total_requests > 0 else 0
        
        return {
            "cache_size": len(self._cache),
            "total_hits": self._total_hits,
            "total_misses": self._total_misses,
            "hit_rate_percent": round(hit_rate, 2),
            "entries": [
                {
                    "key": key,
                    "hit_count": entry.hit_count,
                    "created_at": entry.created_at.isoformat(),
                    "last_access": entry.last_access.isoformat(),
                    "is_expired": entry.is_expired()
                }
                for key, entry in self._cache.items()
            ]
        }
    
    async def warmup(self, cache_entries: Dict[str, tuple]) -> None:
        """
        Pre-aquece o cache com modelos comuns.
        
        Args:
            cache_entries: Dict com cache_key -> (factory_func, args, kwargs)
        """
        tasks = []
        for cache_key, (factory_func, args, kwargs) in cache_entries.items():
            task = asyncio.create_task(
                self.get_or_create(cache_key, factory_func, *args, **kwargs)
            )
            tasks.append(task)
        
        try:
            await asyncio.gather(*tasks, return_exceptions=True)
        except Exception as e:
            app_logger.warning("⚠️ Erro durante warmup do cache", error=str(e))
