import pytest
import os
from unittest.mock import patch
from src.infrastructure.config.app_config import AppConfig, DatabaseConfig


class TestDatabaseConfig:
    """Testes unitários para DatabaseConfig."""
    
    @patch.dict(os.environ, {}, clear=True)
    def test_from_environment_with_default_values(self):
        """Testa criação com valores padrão."""
        # Arrange & Act
        config = DatabaseConfig.from_environment()
        
        # Assert
        assert config.connection_string == 'mongodb://localhost:27017'
        assert config.database_name == 'agno'
    
    @patch.dict(os.environ, {
        'MONGO_CONNECTION_STRING': 'mongodb://test:27017',
        'MONGO_DATABASE_NAME': 'test_db'
    })
    def test_from_environment_with_custom_values(self):
        """Testa criação com valores customizados."""
        # Arrange & Act
        config = DatabaseConfig.from_environment()
        
        # Assert
        assert config.connection_string == 'mongodb://test:27017'
        assert config.database_name == 'test_db'
    
    def test_create_database_config_directly(self):
        """Testa criação direta de DatabaseConfig."""
        # Arrange & Act
        config = DatabaseConfig(
            connection_string='mongodb://direct:27017',
            database_name='direct_db'
        )
        
        # Assert
        assert config.connection_string == 'mongodb://direct:27017'
        assert config.database_name == 'direct_db'


class TestAppConfig:
    """Testes unitários para AppConfig."""
    
    @patch.dict(os.environ, {}, clear=True)
    def test_load_with_default_values(self):
        """Testa carregamento com valores padrão."""
        # Arrange & Act
        config = AppConfig.load()
        
        # Assert
        assert config.app_title == 'Orquestrador agno'
        assert isinstance(config.database, DatabaseConfig)
        assert config.database.connection_string == 'mongodb://localhost:27017'
        assert config.database.database_name == 'agno'
    
    @patch.dict(os.environ, {
        'APP_TITLE': 'Custom Orquestrador',
        'MONGO_CONNECTION_STRING': 'mongodb://custom:27017',
        'MONGO_DATABASE_NAME': 'custom_db'
    })
    def test_load_with_custom_values(self):
        """Testa carregamento com valores customizados."""
        # Arrange & Act
        config = AppConfig.load()
        
        # Assert
        assert config.app_title == 'Custom Orquestrador'
        assert config.database.connection_string == 'mongodb://custom:27017'
        assert config.database.database_name == 'custom_db'
    
    def test_create_app_config_directly(self):
        """Testa criação direta de AppConfig."""
        # Arrange
        db_config = DatabaseConfig(
            connection_string='mongodb://direct:27017',
            database_name='direct_db'
        )
        
        # Act
        config = AppConfig(
            app_title='Direct App',
            database=db_config
        )
        
        # Assert
        assert config.app_title == 'Direct App'
        assert config.database == db_config
