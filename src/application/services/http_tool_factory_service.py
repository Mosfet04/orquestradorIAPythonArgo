import httpx
from typing import List, Dict, Any, Optional
import ast
from agno.tools import Toolkit
from src.domain.entities.tool import Tool, HttpMethod


class HttpToolFactory:
    """Factory para criar tools HTTP compatíveis com agno."""
    
    def create_tools_from_configs(self, tools: List[Tool]) -> List[Toolkit]:
        """Cria uma lista de ferramentas agno a partir das configurações."""
        agno_tools = []
        
        for tool in tools:
            agno_tool = self._create_agno_tool(tool)
            agno_tools.append(agno_tool)
        
        return agno_tools
    
    def _create_agno_tool(self, tool: Tool) -> Toolkit:
        """Cria uma ferramenta agno individual."""
        
        def http_function(**kwargs) -> str:
            """Função que executa a requisição HTTP."""
            try:
                # Preparar headers
                headers = tool.headers or {}
                headers.setdefault("Content-Type", "application/json")
                
                # Processar URL para substituir placeholders
                url = tool.route
                remaining_params = kwargs.copy()
                
                # Substituir placeholders na URL (ex: {id}, {user_id})
                for key, value in kwargs.items():
                    if isinstance(value, str):
                        param_dict = ast.literal_eval(value)
                    else:
                        param_dict = value
                    
                    # Capturar o primeiro par chave-valor (independente do formato)
                    if isinstance(param_dict, dict) and param_dict:
                        param_key = next(iter(param_dict))
                        param_value = param_dict[param_key]
                        placeholder = f"{{{param_key}}}"
                    if placeholder in url:
                        url = url.replace(placeholder, str(param_value))
                        remaining_params.pop(key)  # Remover da lista de query params
                
                # Preparar dados baseado no método HTTP
                if tool.http_method in [HttpMethod.GET, HttpMethod.DELETE]:
                    # Para GET e DELETE, usar params restantes na URL
                    response = httpx.request(
                        method=tool.http_method.value,
                        url=url,
                        params=remaining_params if remaining_params else None,
                        headers=headers,
                        timeout=30.0
                    )
                else:
                    # Para POST, PUT, PATCH, usar body JSON com parâmetros restantes
                    response = httpx.request(
                        method=tool.http_method.value,
                        url=url,
                        json=remaining_params if remaining_params else None,
                        headers=headers,
                        timeout=30.0
                    )
                
                response.raise_for_status()
                
                # Tentar retornar JSON, senão retornar texto
                try:
                    return str(response.json())
                except:
                    return response.text
                    
            except httpx.RequestError as e:
                return f"Erro na requisição: {str(e)}"
            except httpx.HTTPStatusError as e:
                return f"Erro HTTP {e.response.status_code}: {e.response.text}"
            except Exception as e:
                return f"Erro inesperado: {str(e)}"
        
        # Criar a descrição da função para o agno
        function_description = self._create_function_description(tool)
        
        # Criar toolkit do agno
        toolkit = Toolkit(name=tool.name, instructions=function_description)
        toolkit.register(
            function=http_function,
            name=tool.name,
        )
        return toolkit
    
    def _create_function_description(self, tool: Tool) -> str:
        """Cria a descrição da função para o agno."""
        description = tool.description
        
        if tool.instructions:
            description += f"\n\nInstruções: {tool.instructions}"
        
        description += f"\n\nRota: {tool.http_method.value} {tool.route}"
        
        if tool.parameters:
            description += "\n\nParâmetros disponíveis:"
            for param in tool.parameters:
                required_text = " (obrigatório)" if param.required else " (opcional)"
                description += f"\n- {param.name}: {param.description}{required_text}"
        
        return description
    
    def _create_parameters_schema(self, tool: Tool) -> Dict[str, Any]:
        """Cria o schema de parâmetros para o agno."""
        if not tool.parameters:
            return {}
        
        properties = {}
        required = []
        
        for param in tool.parameters:
            # Mapear tipos para JSON Schema
            param_type = self._map_parameter_type(param.type.value)
            
            properties[param.name] = {
                "type": param_type,
                "description": param.description
            }
            
            if param.default_value is not None:
                properties[param.name]["default"] = param.default_value
            
            if param.required:
                required.append(param.name)
        
        schema = {
            "type": "object",
            "properties": properties
        }
        
        if required:
            schema["required"] = required
        
        return schema
    
    def _map_parameter_type(self, param_type: str) -> str:
        """Mapeia tipos de parâmetro para tipos JSON Schema."""
        type_mapping = {
            "string": "string",
            "integer": "integer",
            "float": "number",
            "boolean": "boolean",
            "object": "object",
            "array": "array"
        }
        return type_mapping.get(param_type, "string")
