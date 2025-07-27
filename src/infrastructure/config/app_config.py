import os
from dataclasses import dataclass


@dataclass
class DatabaseConfig:
    """Configuração do banco de dados."""
    connection_string: str
    database_name: str
    
    @classmethod
    def from_environment(cls) -> 'DatabaseConfig':
        """Cria configuração a partir de variáveis de ambiente."""
        return cls(
            connection_string=os.getenv('MONGO_CONNECTION_STRING', 'mongodb://localhost:27017'),
            database_name=os.getenv('MONGO_DATABASE_NAME', 'agno')
        )


@dataclass
class AppConfig:
    """Configuração da aplicação."""
    app_title: str
    database: DatabaseConfig
    
    @classmethod
    def load(cls) -> 'AppConfig':
        """Carrega a configuração da aplicação."""
        return cls(
            app_title=os.getenv('APP_TITLE', 'Orquestrador agno'),
            database=DatabaseConfig.from_environment()
        )
