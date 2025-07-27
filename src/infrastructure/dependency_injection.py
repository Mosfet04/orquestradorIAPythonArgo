from src.domain.repositories.agent_config_repository import IAgentConfigRepository
from src.application.services.agent_factory_service import AgentFactoryService
from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase
from src.presentation.controllers.orquestrador_controller import OrquestradorController
from src.infrastructure.repositories.mongo_agent_config_repository import MongoAgentConfigRepository
from src.infrastructure.config.app_config import AppConfig


class DependencyContainer:
    """Container para injeção de dependências."""
    
    def __init__(self, config: AppConfig):
        self._config = config
        self._repositories = {}
        self._services = {}
        self._use_cases = {}
        self._controllers = {}
    
    def get_agent_config_repository(self) -> IAgentConfigRepository:
        """Obtém o repositório de configurações de agentes."""
        if 'agent_config' not in self._repositories:
            self._repositories['agent_config'] = MongoAgentConfigRepository(
                connection_string=self._config.database.connection_string,
                database_name=self._config.database.database_name
            )
        return self._repositories['agent_config']
    
    def get_agent_factory_service(self) -> AgentFactoryService:
        """Obtém o serviço de criação de agentes."""
        if 'agent_factory' not in self._services:
            self._services['agent_factory'] = AgentFactoryService(
                db_url=self._config.database.connection_string,
                db_name=self._config.database.database_name
            )
        return self._services['agent_factory']
    
    def get_active_agents_use_case(self) -> GetActiveAgentsUseCase:
        """Obtém o caso de uso de agentes ativos."""
        if 'get_active_agents' not in self._use_cases:
            self._use_cases['get_active_agents'] = GetActiveAgentsUseCase(
                agent_config_repository=self.get_agent_config_repository(),
                agent_factory_service=self.get_agent_factory_service()
            )
        return self._use_cases['get_active_agents']
    
    def get_orquestrador_controller(self) -> OrquestradorController:
        """Obtém o controller do orquestrador."""
        if 'orquestrador' not in self._controllers:
            self._controllers['orquestrador'] = OrquestradorController(
                get_active_agents_use_case=self.get_active_agents_use_case()
            )
        return self._controllers['orquestrador']
