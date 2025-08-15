from typing import List
from pymongo import MongoClient
from pymongo.errors import ConnectionFailure, ServerSelectionTimeoutError
from src.domain.entities.tool import Tool, ToolParameter, HttpMethod, ParameterType
from src.domain.repositories.tool_repository import IToolRepository
from src.infrastructure.logging import LoggerFactory, log_execution, log_performance
import os


class MongoToolRepository(IToolRepository):
    """Implementação do repositório de tools usando MongoDB."""
    
    def __init__(self, connection_string: str = "mongodb://localhost:27017", 
                 database_name: str = "agno", 
                 collection_name: str = "tools"):
        self._connection_string = connection_string
        self._database_name = database_name
        self._collection_name = collection_name
        self._client = None
        self._db = None
        self._collection = None
        
        # Configuração TLS/SSL para MongoDB Atlas
        self._use_tls = self._should_use_tls()
        self._tls_allow_invalid_certificates = os.getenv("TLS_ALLOW_INVALID_CERTIFICATES", "false").lower() == "true"
        
        self.logger = LoggerFactory.get_logger("mongo_tool_repository")
    
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
                self.logger.info(f"Conectando ao MongoDB: {self._connection_string[:50]}... (TLS: {self._use_tls})")
                
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
                self.logger.info("Conexão com MongoDB estabelecida com sucesso")
                
                self._db = self._client[self._database_name]
                self._collection = self._db[self._collection_name]
                
            except (ConnectionFailure, ServerSelectionTimeoutError) as e:
                self.logger.error(f"Erro ao conectar ao MongoDB: {str(e)}")
                raise ConnectionError(f"Não foi possível conectar ao MongoDB: {str(e)}")
            except Exception as e:
                self.logger.error(f"Erro inesperado ao conectar ao MongoDB: {str(e)}")
                raise
        
        return self._collection
    
    @log_execution(logger_name="mongo_tool_repository")
    @log_performance(threshold_seconds=1.0)
    def get_tools_by_ids(self, tool_ids: List[str]) -> List[Tool]:
        """Retorna lista de tools pelos IDs fornecidos."""
        try:
            if not tool_ids:
                return []
                
            collection = self._get_collection()
            query = {"id": {"$in": tool_ids}, "active": True}
            results = collection.find(query)
            
            tools = []
            for tool_data in results:
                tool = self._map_to_entity(tool_data)
                tools.append(tool)
            
            return tools
        except Exception as e:
            self.logger.error(f"Erro ao buscar tools por IDs {tool_ids}: {str(e)}")
            raise
    
    def get_tool_by_id(self, tool_id: str) -> Tool:
        """Retorna uma tool por ID."""
        try:
            collection = self._get_collection()
            query = {"id": tool_id}
            tool_data = collection.find_one(query)
            
            if not tool_data:
                raise ValueError(f"Tool com ID {tool_id} não encontrada")
            
            return self._map_to_entity(tool_data)
        except Exception as e:
            self.logger.error(f"Erro ao buscar tool por ID {tool_id}: {str(e)}")
            raise
    
    def get_all_active_tools(self) -> List[Tool]:
        """Retorna todas as tools ativas."""
        collection = self._get_collection()
        query = {"active": True}
        results = collection.find(query)
        
        tools = []
        for tool_data in results:
            tool = self._map_to_entity(tool_data)
            tools.append(tool)
        
        return tools
    
    def _map_to_entity(self, tool_data: dict) -> Tool:
        """Mapeia dados do banco para a entidade Tool."""
        # Mapear parâmetros
        parameters = []
        for param_data in tool_data.get("parameters", []):
            parameter = ToolParameter(
                name=param_data.get("name"),
                type=ParameterType(param_data.get("type")),
                description=param_data.get("description"),
                required=param_data.get("required", False),
                default_value=param_data.get("default_value")
            )
            parameters.append(parameter)
        
        return Tool(
            id=tool_data.get("id", ""),
            name=tool_data.get("name", ""),
            description=tool_data.get("description", ""),
            route=tool_data.get("route", ""),
            http_method=HttpMethod(tool_data.get("http_method", "GET")),
            parameters=parameters,
            instructions=tool_data.get("instructions", ""),
            headers=tool_data.get("headers", {}),
            active=tool_data.get("active", True)
        )
