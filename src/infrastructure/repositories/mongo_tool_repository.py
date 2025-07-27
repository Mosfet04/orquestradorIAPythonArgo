from typing import List
from pymongo import MongoClient
from src.domain.entities.tool import Tool, ToolParameter, HttpMethod, ParameterType
from src.domain.repositories.tool_repository import IToolRepository


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
    
    def _get_collection(self):
        """Obtém a coleção do MongoDB de forma lazy."""
        if self._collection is None:
            self._client = MongoClient(self._connection_string)
            self._db = self._client[self._database_name]
            self._collection = self._db[self._collection_name]
        return self._collection
    
    def get_tools_by_ids(self, tool_ids: List[str]) -> List[Tool]:
        """Retorna lista de tools pelos IDs fornecidos."""
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
    
    def get_tool_by_id(self, tool_id: str) -> Tool:
        """Retorna uma tool por ID."""
        collection = self._get_collection()
        query = {"id": tool_id}
        tool_data = collection.find_one(query)
        
        if not tool_data:
            raise ValueError(f"Tool com ID {tool_id} não encontrada")
        
        return self._map_to_entity(tool_data)
    
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
            id=tool_data.get("id"),
            name=tool_data.get("name"),
            description=tool_data.get("description"),
            route=tool_data.get("route"),
            http_method=HttpMethod(tool_data.get("http_method")),
            parameters=parameters,
            instructions=tool_data.get("instructions"),
            headers=tool_data.get("headers", {}),
            active=tool_data.get("active", True)
        )
