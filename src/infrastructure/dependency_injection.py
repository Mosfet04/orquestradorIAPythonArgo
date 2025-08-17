import asyncio
from typing import Optional
from motor.motor_asyncio import AsyncIOMotorClient
from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.repositories.mongo_agent_config_repository import MongoAgentConfigRepository  
from src.infrastructure.repositories.mongo_tool_repository import MongoToolRepository
from src.application.services.agent_factory_service import AgentFactoryService
from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase
from src.presentation.controllers.orquestrador_controller import OrquestradorController
from src.infrastructure.logging import app_logger


class HealthService:
    """Serviço de health check otimizado."""
    
    def __init__(self, mongo_client: AsyncIOMotorClient):
        self._mongo_client = mongo_client
    
    async def check_async(self) -> dict:
        """Verifica saúde da aplicação assincronamente."""
        start_time = asyncio.get_event_loop().time()
        
        checks = await asyncio.gather(
            self._check_mongodb(),
            self._check_memory(),
            return_exceptions=True
        )
        
        total_time = asyncio.get_event_loop().time() - start_time
        
        return {
            "status": "healthy" if all(not isinstance(c, Exception) for c in checks) else "unhealthy",
            "checks": {
                "mongodb": checks[0] if not isinstance(checks[0], Exception) else {"status": "error", "error": str(checks[0])},
                "memory": checks[1] if not isinstance(checks[1], Exception) else {"status": "error", "error": str(checks[1])},
            },
            "response_time_ms": round(total_time * 1000, 2)
        }
    
    async def _check_mongodb(self) -> dict:
        """Verifica conexão MongoDB."""
        try:
            await self._mongo_client.admin.command('ping')
            return {"status": "healthy"}
        except Exception as e:
            return {"status": "unhealthy", "error": str(e)}
    
    async def _check_memory(self) -> dict:
        """Verifica uso de memória."""
        try:
            import psutil
            memory = psutil.virtual_memory()
            return {
                "status": "healthy" if memory.percent < 90 else "warning",
                "usage_percent": memory.percent,
                "available_gb": round(memory.available / (1024**3), 2)
            }
        except ImportError:
            return {
                "status": "unavailable",
                "message": "psutil não está instalado"
            }


class DependencyContainer:
    """Container de dependências otimizado para performance."""
    
    def __init__(self, config: AppConfig):
        self.config = config
        self._mongo_client: Optional[AsyncIOMotorClient] = None
        self._agent_config_repository: Optional[MongoAgentConfigRepository] = None
        self._tool_repository: Optional[MongoToolRepository] = None
        self._agent_factory_service: Optional[AgentFactoryService] = None
        self._get_active_agents_use_case: Optional[GetActiveAgentsUseCase] = None
        self._orquestrador_controller: Optional[OrquestradorController] = None
        self._health_service: Optional[HealthService] = None
    
    @classmethod
    async def create_async(cls, config: AppConfig) -> "DependencyContainer":
        """Factory assíncrono para criar container com conexões otimizadas."""
        container = cls(config)
        await container._initialize_async()
        return container
    
    async def _initialize_async(self) -> None:
        """Inicializa todas as dependências assincronamente."""
        app_logger.info("🔧 Inicializando container de dependências")
        
        # Inicializar cliente MongoDB com pool otimizado
        self._mongo_client = AsyncIOMotorClient(
            self.config.mongo_connection_string,
            maxPoolSize=20,  # Pool otimizado
            minPoolSize=5,
            connectTimeoutMS=5000,
            serverSelectionTimeoutMS=5000
        )
        
        # Testar conexão (opcional em desenvolvimento)
        try:
            if self._mongo_client:
                await self._mongo_client.admin.command('ping')
                app_logger.info("✅ Conexão MongoDB estabelecida")
        except Exception as e:
            app_logger.warning("⚠️ MongoDB não disponível - modo desenvolvimento", error=str(e))
            # Em desenvolvimento, continuar sem MongoDB
            # self._mongo_client = None  # Comentado para permitir desenvolvimento
        
        # Inicializar health service
        if self._mongo_client:
            self._health_service = HealthService(self._mongo_client)
        
        app_logger.info("✅ Container inicializado com sucesso")
    
    async def get_orquestrador_controller_async(self) -> OrquestradorController:
        """Obtém controller assincronamente com lazy loading."""
        if not self._orquestrador_controller:
            use_case = await self._get_active_agents_use_case_async()
            self._orquestrador_controller = OrquestradorController(use_case)
        
        return self._orquestrador_controller
    
    async def _get_active_agents_use_case_async(self) -> GetActiveAgentsUseCase:
        """Obtém use case assincronamente."""
        if not self._get_active_agents_use_case:
            agent_factory = await self._get_agent_factory_service_async()
            agent_repository = await self._get_agent_config_repository_async()
            
            self._get_active_agents_use_case = GetActiveAgentsUseCase(
                agent_factory, agent_repository
            )
        
        return self._get_active_agents_use_case
    
    async def _get_agent_factory_service_async(self) -> AgentFactoryService:
        """Obtém factory service assincronamente."""
        if not self._agent_factory_service:
            tool_repository = await self._get_tool_repository_async()
            self._agent_factory_service = AgentFactoryService(
                db_url=self.config.mongo_connection_string,
                db_name=self.config.mongo_database_name,
                tool_repository=tool_repository
            )
        
        return self._agent_factory_service
    
    async def _get_agent_config_repository_async(self) -> MongoAgentConfigRepository:
        """Obtém repository de configuração assincronamente."""
        if not self._agent_config_repository:
            try:
                self._agent_config_repository = MongoAgentConfigRepository(
                    connection_string=self.config.mongo_connection_string,
                    database_name=self.config.mongo_database_name
                )
            except Exception as e:
                app_logger.warning("⚠️ Usando repository mock para desenvolvimento", error=str(e))
                # TODO: Implementar MockAgentConfigRepository
                self._agent_config_repository = MongoAgentConfigRepository(
                    connection_string="mongodb://localhost:27017",
                    database_name="mock_db"
                )
        
        return self._agent_config_repository
    
    async def _get_tool_repository_async(self) -> MongoToolRepository:
        """Obtém tool repository assincronamente."""
        if not self._tool_repository:
            try:
                self._tool_repository = MongoToolRepository(
                    connection_string=self.config.mongo_connection_string,
                    database_name=self.config.mongo_database_name
                )
            except Exception as e:
                app_logger.warning("⚠️ Usando tool repository mock para desenvolvimento", error=str(e))
                # TODO: Implementar MockToolRepository
                self._tool_repository = MongoToolRepository(
                    connection_string="mongodb://localhost:27017",
                    database_name="mock_db"
                )
        
        return self._tool_repository
    
    @property
    def health_service(self) -> Optional[HealthService]:
        """Propriedade para acessar health service."""
        return self._health_service
    
    async def cleanup(self) -> None:
        """Cleanup assíncrono de recursos."""
        app_logger.info("🧹 Limpando recursos do container")
        
        if self._mongo_client:
            self._mongo_client.close()
        
        app_logger.info("✅ Cleanup concluído")