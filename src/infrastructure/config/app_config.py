import asyncio
import os
from dataclasses import dataclass
from typing import Optional
from src.infrastructure.logging import app_logger


@dataclass
class AppConfig:
    """Configuração da aplicação com carregamento assíncrono."""
    
    mongo_connection_string: str
    mongo_database_name: str
    app_title: str
    app_host: str
    app_port: int
    log_level: str
    ollama_base_url: str
    openai_api_key: Optional[str] = None
    
    @classmethod
    async def load_async(cls) -> "AppConfig":
        """Carrega configurações assincronamente com validação."""
        
        # Simular operação assíncrona se necessário (ex: buscar de API externa)
        await asyncio.sleep(0.001)  # Placeholder para futuras operações async
        
        config = cls(
            mongo_connection_string=os.getenv(
                "MONGO_CONNECTION_STRING", 
                "mongodb://localhost:27017"
            ),
            mongo_database_name=os.getenv("MONGO_DATABASE_NAME", "agno"),
            app_title=os.getenv("APP_TITLE", "Orquestrador IA Otimizado"),
            app_host=os.getenv("APP_HOST", "0.0.0.0"),
            app_port=int(os.getenv("APP_PORT", "7777")),
            log_level=os.getenv("LOG_LEVEL", "INFO"),
            ollama_base_url=os.getenv("OLLAMA_BASE_URL", "http://localhost:11434"),
            openai_api_key=os.getenv("OPENAI_API_KEY")
        )
        
        # Validação assíncrona
        await config._validate_async()
        
        return config
    
    @classmethod
    def load(cls) -> "AppConfig":
        """Método síncrono mantido para compatibilidade."""
        return asyncio.run(cls.load_async())
    
    async def _validate_async(self) -> None:
        """Valida configurações assincronamente."""
        if not self.mongo_connection_string:
            raise ValueError("MONGO_CONNECTION_STRING é obrigatória")
        
        if not self.mongo_database_name:
            raise ValueError("MONGO_DATABASE_NAME é obrigatório")
