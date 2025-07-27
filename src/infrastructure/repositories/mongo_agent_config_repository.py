from typing import List
from pymongo import MongoClient
from src.domain.entities.agent_config import AgentConfig
from src.domain.repositories.agent_config_repository import IAgentConfigRepository


class MongoAgentConfigRepository(IAgentConfigRepository):
    """Implementação do repositório de configurações de agentes usando MongoDB."""
    
    def __init__(self, connection_string: str = "mongodb://localhost:27017", 
                 database_name: str = "agno", 
                 collection_name: str = "agents_config"):
        self._connection_string = connection_string
        self._database_name = database_name
        self._collection_name = collection_name
        self._client = None
        self._db = None
        self._collection = None
    
    def _get_collection(self):
        """Obtém a coleção do MongoDB de forma lazy."""
        if self._collection is None:
            self._client = MongoClient(self._connection_string)
            self._db = self._client[self._database_name]
            self._collection = self._db[self._collection_name]
        return self._collection
    
    def get_active_agents(self) -> List[AgentConfig]:
        """Retorna lista de agentes ativos."""
        collection = self._get_collection()
        query = {"active": True}
        resultados = collection.find(query)
        
        agents = []
        for agent_data in resultados:
            agent_config = self._map_to_entity(agent_data)
            agents.append(agent_config)
        
        return agents
    
    def get_agent_by_id(self, agent_id: str) -> AgentConfig:
        """Retorna um agente por ID."""
        collection = self._get_collection()
        query = {"id": agent_id}
        agent_data = collection.find_one(query)
        
        if not agent_data:
            raise ValueError(f"Agente com ID {agent_id} não encontrado")
        
        return self._map_to_entity(agent_data)
    
    def _map_to_entity(self, agent_data: dict) -> AgentConfig:
        """Mapeia dados do banco para a entidade AgentConfig."""
        return AgentConfig(
            id=agent_data.get("id"),
            nome=agent_data.get("nome"),
            model=agent_data.get("model"),
            descricao=agent_data.get("descricao"),
            prompt=agent_data.get("prompt"),
            active=agent_data.get("active", True)
        )
