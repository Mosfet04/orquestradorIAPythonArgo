import pytest
import os
import asyncio
from unittest.mock import patch
from src.infrastructure.config.app_config import AppConfig


class TestAppConfig:
    """Testes unitários para AppConfig."""
    
    @patch.dict(os.environ, {}, clear=True)
    def test_load_with_default_values(self):
        """Testa carregamento síncrono com valores padrão."""
        # Arrange & Act
        config = AppConfig.load()
        
        # Assert
        assert config.app_title == 'Orquestrador IA Otimizado'
        assert config.mongo_connection_string == 'mongodb://localhost:62659/?directConnection=true'
        assert config.mongo_database_name == 'agno'
        assert config.app_host == '0.0.0.0'
        assert config.app_port == 7777
        assert config.log_level == 'INFO'
        assert config.ollama_base_url == 'http://localhost:11434'
        assert config.openai_api_key is None
    
    @patch.dict(os.environ, {
        'APP_TITLE': 'Custom Orquestrador',
        'MONGO_CONNECTION_STRING': 'mongodb://custom:27017',
        'MONGO_DATABASE_NAME': 'custom_db',
        'APP_HOST': '127.0.0.1',
        'APP_PORT': '8888',
        'LOG_LEVEL': 'DEBUG',
        'OLLAMA_BASE_URL': 'http://custom-ollama:11434',
        'OPENAI_API_KEY': 'test-key'
    })
    def test_load_with_custom_values(self):
        """Testa carregamento síncrono com valores customizados."""
        # Arrange & Act
        config = AppConfig.load()
        
        # Assert
        assert config.app_title == 'Custom Orquestrador'
        assert config.mongo_connection_string == 'mongodb://custom:27017'
        assert config.mongo_database_name == 'custom_db'
        assert config.app_host == '127.0.0.1'
        assert config.app_port == 8888
        assert config.log_level == 'DEBUG'
        assert config.ollama_base_url == 'http://custom-ollama:11434'
        assert config.openai_api_key == 'test-key'
    
    @pytest.mark.asyncio
    async def test_load_async_with_default_values(self):
        """Testa carregamento assíncrono com valores padrão."""
        with patch.dict(os.environ, {}, clear=True):
            # Arrange & Act
            config = await AppConfig.load_async()
            
            # Assert
            assert config.app_title == 'Orquestrador IA Otimizado'
            assert config.mongo_connection_string == 'mongodb://localhost:62659/?directConnection=true'
            assert config.mongo_database_name == 'agno'
            assert config.app_host == '0.0.0.0'
            assert config.app_port == 7777
            assert config.log_level == 'INFO'
            assert config.ollama_base_url == 'http://localhost:11434'
            assert config.openai_api_key is None
    
    @pytest.mark.asyncio
    async def test_load_async_with_custom_values(self):
        """Testa carregamento assíncrono com valores customizados."""
        with patch.dict(os.environ, {
            'APP_TITLE': 'Async Custom Orquestrador',
            'MONGO_CONNECTION_STRING': 'mongodb://async-custom:27017',
            'MONGO_DATABASE_NAME': 'async_custom_db'
        }):
            # Arrange & Act
            config = await AppConfig.load_async()
            
            # Assert
            assert config.app_title == 'Async Custom Orquestrador'
            assert config.mongo_connection_string == 'mongodb://async-custom:27017'
            assert config.mongo_database_name == 'async_custom_db'
    
    def test_create_app_config_directly(self):
        """Testa criação direta de AppConfig."""
        # Arrange & Act
        config = AppConfig(
            mongo_connection_string='mongodb://direct:27017',
            mongo_database_name='direct_db',
            app_title='Direct App',
            app_host='localhost',
            app_port=9999,
            log_level='ERROR',
            ollama_base_url='http://direct-ollama:11434',
            openai_api_key='direct-key'
        )
        
        # Assert
        assert config.mongo_connection_string == 'mongodb://direct:27017'
        assert config.mongo_database_name == 'direct_db'
        assert config.app_title == 'Direct App'
        assert config.app_host == 'localhost'
        assert config.app_port == 9999
        assert config.log_level == 'ERROR'
        assert config.ollama_base_url == 'http://direct-ollama:11434'
        assert config.openai_api_key == 'direct-key'
    
    @pytest.mark.asyncio
    async def test_validation_error_empty_connection_string(self):
        """Testa erro de validação com connection string vazia."""
        with patch.dict(os.environ, {'MONGO_CONNECTION_STRING': ''}):
            # Arrange & Act & Assert
            with pytest.raises(ValueError, match="MONGO_CONNECTION_STRING é obrigatória"):
                await AppConfig.load_async()
    
    @pytest.mark.asyncio
    async def test_validation_error_empty_database_name(self):
        """Testa erro de validação com database name vazio."""
        with patch.dict(os.environ, {'MONGO_DATABASE_NAME': ''}):
            # Arrange & Act & Assert
            with pytest.raises(ValueError, match="MONGO_DATABASE_NAME é obrigatório"):
                await AppConfig.load_async()
