import pytest
import asyncio
from unittest.mock import Mock, AsyncMock
from datetime import datetime, timedelta
from src.infrastructure.cache.model_cache_service import ModelCacheService, ModelCacheEntry


class TestModelCacheEntry:
    """Testes unitários para ModelCacheEntry."""
    
    def test_create_cache_entry_with_default_ttl(self):
        """Testa criação de entrada de cache com TTL padrão."""
        # Arrange
        mock_model = Mock()
        
        # Act
        entry = ModelCacheEntry(mock_model)
        
        # Assert
        assert entry.model == mock_model
        assert entry.hit_count == 0
        assert entry.ttl == timedelta(minutes=30)
        assert not entry.is_expired()
    
    def test_create_cache_entry_with_custom_ttl(self):
        """Testa criação de entrada de cache com TTL customizado."""
        # Arrange
        mock_model = Mock()
        custom_ttl = 60
        
        # Act
        entry = ModelCacheEntry(mock_model, custom_ttl)
        
        # Assert
        assert entry.model == mock_model
        assert entry.ttl == timedelta(minutes=custom_ttl)
    
    def test_is_expired_returns_false_for_fresh_entry(self):
        """Testa que entrada recém criada não está expirada."""
        # Arrange
        mock_model = Mock()
        entry = ModelCacheEntry(mock_model, ttl_minutes=1)
        
        # Act & Assert
        assert not entry.is_expired()
    
    def test_is_expired_returns_true_for_old_entry(self):
        """Testa que entrada antiga está expirada."""
        # Arrange
        mock_model = Mock()
        entry = ModelCacheEntry(mock_model, ttl_minutes=1)
        # Simular entrada antiga
        entry.created_at = datetime.utcnow() - timedelta(minutes=2)
        
        # Act & Assert
        assert entry.is_expired()
    
    def test_access_increments_hit_count_and_updates_last_access(self):
        """Testa que acesso incrementa contador e atualiza último acesso."""
        # Arrange
        mock_model = Mock()
        entry = ModelCacheEntry(mock_model)
        initial_hit_count = entry.hit_count
        
        # Act
        result = entry.access()
        
        # Assert
        assert result == mock_model
        assert entry.hit_count == initial_hit_count + 1
        # Note: não testamos last_access pois pode ser igual devido à precisão de tempo
    
    def test_multiple_accesses_increment_hit_count(self):
        """Testa que múltiplos acessos incrementam contador corretamente."""
        # Arrange
        mock_model = Mock()
        entry = ModelCacheEntry(mock_model)
        
        # Act
        entry.access()
        entry.access()
        entry.access()
        
        # Assert
        assert entry.hit_count == 3


class TestModelCacheService:
    """Testes unitários para ModelCacheService."""
    
    def setup_method(self):
        """Setup executado antes de cada teste."""
        self.cache_service = ModelCacheService()
    
    def test_cache_service_initialization_with_defaults(self):
        """Testa inicialização do serviço com valores padrão."""
        # Assert
        assert self.cache_service._ttl_minutes == 30
        assert self.cache_service._total_hits == 0
        assert self.cache_service._total_misses == 0
        assert len(self.cache_service._cache) == 0
    
    def test_cache_service_initialization_with_custom_ttl(self):
        """Testa inicialização do serviço com TTL customizado."""
        # Arrange & Act
        custom_ttl = 60
        cache_service = ModelCacheService(ttl_minutes=custom_ttl)
        
        # Assert
        assert cache_service._ttl_minutes == custom_ttl
    
    @pytest.mark.asyncio
    async def test_get_or_create_with_cache_miss(self):
        """Testa get_or_create com cache miss."""
        # Arrange
        cache_key = "test_model"
        mock_model = Mock()
        mock_factory = Mock(return_value=mock_model)
        
        # Act
        result = await self.cache_service.get_or_create(cache_key, mock_factory)
        
        # Assert
        assert result == mock_model
        mock_factory.assert_called_once()
        assert cache_key in self.cache_service._cache
        assert self.cache_service._total_misses == 1
        assert self.cache_service._total_hits == 0
    
    @pytest.mark.asyncio
    async def test_get_or_create_with_cache_hit(self):
        """Testa get_or_create com cache hit."""
        # Arrange
        cache_key = "test_model"
        mock_model = Mock()
        mock_factory = Mock(return_value=mock_model)
        
        # Primeira chamada para popular cache
        await self.cache_service.get_or_create(cache_key, mock_factory)
        mock_factory.reset_mock()
        
        # Act - Segunda chamada deve usar cache
        result = await self.cache_service.get_or_create(cache_key, mock_factory)
        
        # Assert
        assert result == mock_model
        mock_factory.assert_not_called()  # Factory não deve ser chamada
        assert self.cache_service._total_hits == 1
        assert self.cache_service._total_misses == 1  # Apenas da primeira chamada
    
    @pytest.mark.asyncio
    async def test_get_or_create_with_expired_entry(self):
        """Testa get_or_create com entrada expirada."""
        # Arrange
        cache_key = "test_model"
        mock_model = Mock()
        mock_factory = Mock(return_value=mock_model)
        
        # Primeira chamada
        await self.cache_service.get_or_create(cache_key, mock_factory)
        
        # Simular expiração
        self.cache_service._cache[cache_key].created_at = datetime.utcnow() - timedelta(hours=1)
        mock_factory.reset_mock()
        
        # Act
        result = await self.cache_service.get_or_create(cache_key, mock_factory)
        
        # Assert
        assert result == mock_model
        mock_factory.assert_called_once()  # Factory deve ser chamada novamente
        assert self.cache_service._total_misses == 2  # Cache miss devido à expiração
    
    @pytest.mark.asyncio
    async def test_get_or_create_with_async_factory(self):
        """Testa get_or_create com factory assíncrono."""
        # Arrange
        cache_key = "test_model"
        mock_model = Mock()
        mock_async_factory = AsyncMock(return_value=mock_model)
        
        # Act
        result = await self.cache_service.get_or_create(cache_key, mock_async_factory)
        
        # Assert
        assert result == mock_model
        mock_async_factory.assert_called_once()
    
    @pytest.mark.asyncio
    async def test_invalidate_specific_key(self):
        """Testa invalidação de chave específica."""
        # Arrange
        cache_key = "test_model"
        mock_model = Mock()
        mock_factory = Mock(return_value=mock_model)
        await self.cache_service.get_or_create(cache_key, mock_factory)
        
        # Act
        await self.cache_service.invalidate(cache_key)
        
        # Assert
        assert cache_key not in self.cache_service._cache
    
    @pytest.mark.asyncio
    async def test_invalidate_all_cache(self):
        """Testa invalidação de todo o cache."""
        # Arrange
        mock_factory = Mock(return_value=Mock())
        await self.cache_service.get_or_create("key1", mock_factory)
        await self.cache_service.get_or_create("key2", mock_factory)
        
        # Act
        await self.cache_service.invalidate()
        
        # Assert
        assert len(self.cache_service._cache) == 0
    
    def test_get_stats(self):
        """Testa obtenção de estatísticas do cache."""
        # Arrange
        self.cache_service._cache["key1"] = ModelCacheEntry(Mock())
        self.cache_service._total_hits = 10
        self.cache_service._total_misses = 5
        
        # Act
        stats = self.cache_service.get_stats()
        
        # Assert
        assert stats["cache_size"] == 1
        assert stats["total_hits"] == 10
        assert stats["total_misses"] == 5
        assert stats["hit_rate_percent"] == 66.67  # 10/(10+5) * 100 = 66.67
        assert "entries" in stats
    
    def test_get_stats_with_no_accesses(self):
        """Testa estatísticas com zero acessos."""
        # Act
        stats = self.cache_service.get_stats()
        
        # Assert
        assert stats["cache_size"] == 0
        assert stats["total_hits"] == 0
        assert stats["total_misses"] == 0
        assert stats["hit_rate_percent"] == 0.0
    
    @pytest.mark.asyncio
    async def test_cleanup_expired_entries(self):
        """Testa limpeza de entradas expiradas."""
        # Arrange
        fresh_entry = ModelCacheEntry(Mock())
        expired_entry = ModelCacheEntry(Mock())
        expired_entry.created_at = datetime.utcnow() - timedelta(hours=1)
        
        self.cache_service._cache["fresh"] = fresh_entry
        self.cache_service._cache["expired"] = expired_entry
        
        # Act
        removed_count = await self.cache_service.cleanup_expired()
        
        # Assert
        assert removed_count == 1
        assert "fresh" in self.cache_service._cache
        assert "expired" not in self.cache_service._cache
    
    @pytest.mark.asyncio
    async def test_warmup_cache(self):
        """Testa pre-aquecimento do cache."""
        # Arrange
        mock_model1 = Mock()
        mock_model2 = Mock()
        mock_factory1 = Mock(return_value=mock_model1)
        mock_factory2 = Mock(return_value=mock_model2)
        
        cache_entries = {
            "model1": (mock_factory1, (), {}),
            "model2": (mock_factory2, (), {})
        }
        
        # Act
        await self.cache_service.warmup(cache_entries)
        
        # Assert
        assert "model1" in self.cache_service._cache
        assert "model2" in self.cache_service._cache
        mock_factory1.assert_called_once()
        mock_factory2.assert_called_once()
