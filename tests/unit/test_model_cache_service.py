"""Testes para ModelCacheService."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.infrastructure.cache.model_cache_service import ModelCacheService


@pytest.fixture
def cache_service(mock_logger):
    return ModelCacheService(logger=mock_logger, ttl_minutes=5)


class TestModelCacheService:
    async def test_initialization(self, cache_service):
        assert cache_service._ttl_minutes == 5
        assert len(cache_service._cache) == 0

    async def test_get_or_create_miss(self, cache_service):
        factory = MagicMock(return_value="model_result")
        result = await cache_service.get_or_create("key1", factory)
        assert result == "model_result"
        assert cache_service._total_misses == 1

    async def test_get_or_create_hit(self, cache_service):
        factory = MagicMock(return_value="model_result")
        await cache_service.get_or_create("key1", factory)
        result = await cache_service.get_or_create("key1", factory)
        assert result == "model_result"
        assert cache_service._total_hits == 1
        assert factory.call_count == 1  # só chamou uma vez

    async def test_get_or_create_async_factory(self, cache_service):
        factory = AsyncMock(return_value="async_model")
        result = await cache_service.get_or_create("key1", factory)
        assert result == "async_model"

    async def test_invalidate_specific_key(self, cache_service):
        factory = MagicMock(return_value="m")
        await cache_service.get_or_create("k1", factory)
        await cache_service.get_or_create("k2", factory)
        await cache_service.invalidate("k1")
        assert "k1" not in cache_service._cache
        assert "k2" in cache_service._cache

    async def test_invalidate_all(self, cache_service):
        factory = MagicMock(return_value="m")
        await cache_service.get_or_create("k1", factory)
        await cache_service.get_or_create("k2", factory)
        await cache_service.invalidate()
        assert len(cache_service._cache) == 0

    async def test_get_stats(self, cache_service):
        factory = MagicMock(return_value="m")
        await cache_service.get_or_create("k1", factory)
        stats = cache_service.get_stats()
        assert stats["cache_size"] == 1
        assert stats["total_misses"] == 1

    async def test_cleanup_expired(self, cache_service):
        factory = MagicMock(return_value="m")
        await cache_service.get_or_create("k1", factory)
        # Forçar expiração
        for entry in cache_service._cache.values():
            from datetime import timedelta
            entry.ttl = timedelta(seconds=-1)
        removed = await cache_service.cleanup_expired()
        assert removed == 1
