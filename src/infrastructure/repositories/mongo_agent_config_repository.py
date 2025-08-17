from typing import List
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.rag_config import RagConfig
from src.domain.repositories.agent_config_repository import IAgentConfigRepository
import os
import logging

logger = logging.getLogger(__name__)

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
        
        # Configuração TLS/SSL para MongoDB Atlas
        self._use_tls = self._should_use_tls()
        self._tls_allow_invalid_certificates = os.getenv("TLS_ALLOW_INVALID_CERTIFICATES", "false").lower() == "true"
    
    def _should_use_tls(self) -> bool:
        """Determina se deve usar TLS baseado na connection string."""
        # MongoDB Atlas sempre requer TLS
        if "mongodb.net" in self._connection_string:
            return True
        
        # Para conexões locais, verificar variável de ambiente
        return os.getenv("USE_TLS", "false").lower() == "true"
    
    def _get_collection(self):
        """Obtém a coleção do MongoDB de forma lazy."""
        if self._collection is None:
            try:
                # Configurações de conexão para MongoDB Atlas
                if self._use_tls:
                    self._client = MongoClient(
                        self._connection_string,
                        serverSelectionTimeoutMS=30000,
                        connectTimeoutMS=30000,
                        socketTimeoutMS=30000,
                        maxPoolSize=10,
                        minPoolSize=1,
                        maxIdleTimeMS=30000,
                        tls=True,
                        tlsAllowInvalidCertificates=self._tls_allow_invalid_certificates,
                        tlsAllowInvalidHostnames=self._tls_allow_invalid_certificates,
                        retryWrites=True,
                        w="majority"
                    )
                else:
                    self._client = MongoClient(
                        self._connection_string,
                        serverSelectionTimeoutMS=30000,
                        connectTimeoutMS=30000,
                        socketTimeoutMS=30000,
                        maxPoolSize=10,
                        minPoolSize=1,
                        maxIdleTimeMS=30000
                    )
                
                # Testar conexão
                self._client.admin.command('ping')
                
                self._db = self._client[self._database_name]
                self._collection = self._db[self._collection_name]
                
            except (ConnectionFailure, ServerSelectionTimeoutError) as e:
                logger.error(f"Erro ao conectar ao MongoDB: {str(e)}")
                raise ConnectionError(f"Não foi possível conectar ao MongoDB: {str(e)}")
            except Exception as e:
                logger.error(f"Erro inesperado ao conectar ao MongoDB: {str(e)}")
                raise
        
        return self._collection
    
    def get_active_agents(self) -> List[AgentConfig]:
        """Retorna lista de agentes ativos."""
        try:
            collection = self._get_collection()
            query = {"active": True}
            resultados = collection.find(query)
            
            agents = []
            for agent_data in resultados:
                agent_config = self._map_to_entity(agent_data)
                agents.append(agent_config)
            
            return agents
        except Exception as e:
            logger.error(f"Erro ao buscar agentes ativos: {str(e)}")
            raise
    
    def get_agent_by_id(self, agent_id: str) -> AgentConfig:
        """Retorna um agente por ID."""
        try:
            collection = self._get_collection()
            query = {"id": agent_id}
            agent_data = collection.find_one(query)
            
            if not agent_data:
                raise ValueError(f"Agente com ID {agent_id} não encontrado")
            
            return self._map_to_entity(agent_data)
        except Exception as e:
            logger.error(f"Erro ao buscar agente por ID {agent_id}: {str(e)}")
            raise
    
    def _map_to_entity(self, agent_data: dict) -> AgentConfig:
        """Mapeia dados do banco para a entidade AgentConfig."""
        return AgentConfig(
            id=agent_data.get("id", ""),
            nome=agent_data.get("nome", ""),
            model=agent_data.get("model", ""),
            factoryIaModel=agent_data.get("factoryIaModel", "ollama"),  # valor padrão
            descricao=agent_data.get("descricao", ""),
            prompt=agent_data.get("prompt", ""),
            active=agent_data.get("active", True),
            tools_ids=agent_data.get("tools_ids", []),
            rag_config=RagConfig(
                active=agent_data.get("rag_config", {}).get("active", False),
                doc_name=agent_data.get("rag_config", {}).get("doc_name"),
                model=agent_data.get("rag_config", {}).get("model", "nomic-embed-text:latest"),
                factoryIaModel=agent_data.get("rag_config", {}).get("factoryIaModel","ollama"),
            ) if agent_data.get("rag_config") else None
        )
