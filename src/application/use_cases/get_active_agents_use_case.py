import asyncio
from typing import List
from agno.agent import Agent
from src.application.services.agent_factory_service import AgentFactoryService
from src.infrastructure.repositories.mongo_agent_config_repository import MongoAgentConfigRepository
from src.infrastructure.logging import app_logger


class GetActiveAgentsUseCase:
    """Use case para obter agentes ativos com otimizações de performance."""
    
    def __init__(
        self, 
        agent_factory_service: AgentFactoryService,
        agent_config_repository: MongoAgentConfigRepository
    ):
        self._agent_factory_service = agent_factory_service
        self._agent_config_repository = agent_config_repository
    
    async def execute_async(self) -> List[Agent]:
        """Executa o caso de uso assincronamente com carregamento paralelo."""
        start_time = asyncio.get_event_loop().time()
        
        try:
            app_logger.info("🔍 Buscando agentes ativos")
            
            # Buscar configurações de agentes assincronamente
            agent_configs = await self._get_agent_configs_async()
            
            if not agent_configs:
                app_logger.warning("⚠️ Nenhum agente ativo encontrado")
                return []
            
            app_logger.info("📋 Configurações carregadas", count=len(agent_configs))
            
            # Criar agentes em paralelo para melhor performance
            agents = await self._create_agents_parallel(agent_configs)
            
            load_time = asyncio.get_event_loop().time() - start_time
            
            app_logger.info(
                "✅ Agentes carregados com sucesso", 
                total_configs=len(agent_configs),
                successful_agents=len(agents),
                load_time_seconds=round(load_time, 3)
            )
            
            return agents
            
        except Exception as e:
            load_time = asyncio.get_event_loop().time() - start_time
            app_logger.error(
                "❌ Erro ao executar use case", 
                error=str(e),
                error_type=e.__class__.__name__,
                load_time_seconds=round(load_time, 3)
            )
            raise
    
    def execute(self) -> List[Agent]:
        """Versão síncrona mantida para compatibilidade."""
        return asyncio.run(self.execute_async())
    
    async def _get_agent_configs_async(self):
        """Busca configurações de agentes de forma assíncrona."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, 
            self._agent_config_repository.get_active_agents
        )
    
    async def _create_agents_parallel(self, agent_configs) -> List[Agent]:
        """Cria agentes em paralelo para melhor performance."""
        loop = asyncio.get_event_loop()
        
        # Criar tasks para execução paralela
        agent_tasks = [
            loop.run_in_executor(
                None,
                self._agent_factory_service.create_agent,
                config
            )
            for config in agent_configs
        ]
        
        # Aguardar criação paralela com tratamento de exceções
        agents_results = await asyncio.gather(*agent_tasks, return_exceptions=True)
        
        # Filtrar agentes criados com sucesso
        successful_agents = []
        for i, result in enumerate(agents_results):
            if isinstance(result, Exception):
                app_logger.error(
                    "❌ Erro ao criar agente", 
                    agent_id=getattr(agent_configs[i], 'id', f'agent_{i}'),
                    error=str(result)
                )
            else:
                successful_agents.append(result)
        
        return successful_agents


# Manter alias para compatibilidade com código existente
AsyncGetActiveAgentsUseCase = GetActiveAgentsUseCase