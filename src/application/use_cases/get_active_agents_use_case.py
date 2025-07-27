from typing import List
from agno.agent import Agent
from src.domain.repositories.agent_config_repository import IAgentConfigRepository
from src.application.services.agent_factory_service import AgentFactoryService


class GetActiveAgentsUseCase:
    """Caso de uso para buscar agentes ativos configurados."""
    
    def __init__(self, 
                 agent_config_repository: IAgentConfigRepository,
                 agent_factory_service: AgentFactoryService):
        self._agent_config_repository = agent_config_repository
        self._agent_factory_service = agent_factory_service
    
    def execute(self) -> List[Agent]:
        """Executa o caso de uso para obter agentes ativos."""
        agent_configs = self._agent_config_repository.get_active_agents()
        agents = []
        
        for config in agent_configs:
            agent = self._agent_factory_service.create_agent(config)
            agents.append(agent)
            
        return agents
