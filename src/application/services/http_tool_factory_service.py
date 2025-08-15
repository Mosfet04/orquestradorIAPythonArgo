import httpx
import os
from typing import List, Dict, Any, Optional
import ast
from agno.tools import Toolkit
from src.domain.entities.tool import Tool, HttpMethod
from src.infrastructure.logging import (
    LoggerFactory, 
    log_execution, 
    log_http_request,
    log_performance,
    app_logger
)
import os


class HttpToolFactory:
    """Factory para criar tools HTTP compatíveis com agno."""
    
    def __init__(self):
        self.logger = LoggerFactory.get_logger("http_tool_factory")
    
    @log_execution(logger_name="http_tool_factory", include_args=True)
    @log_performance(threshold_seconds=2.0)
    def create_tools_from_configs(self, tools: List[Tool]) -> List[Toolkit]:
        """Cria uma lista de ferramentas agno a partir das configurações."""
        
        agno_tools = []
        
        for tool in tools:
            try:
                agno_tool = self._create_agno_tool(tool)
                agno_tools.append(agno_tool)

            except Exception as e:
                self.logger.error(f"Erro ao criar ferramenta: {tool.id}",
                    exception= e,
                    tool_id= tool.id,
                    tool_name= tool.name,
                    error_type= e.__class__.__name__
                )
                # Continuar com as outras ferramentas mesmo em caso de erro
                continue
        
        
        return agno_tools
    
    def _create_agno_tool(self, tool: Tool) -> Toolkit:
        """Cria uma ferramenta agno individual."""
        

        
        @log_http_request(logger_name="http_tool_executions")
        def http_function(**kwargs) -> str:
            """Função que executa a requisição HTTP."""
            request_id = f"{tool.id}_{id(kwargs)}"

            self.logger.info(f"Executando requisição HTTP para: {tool.id}",
                tool_id= tool.id,
                request_id= request_id,
                method= tool.http_method.value,
                parameter_count= len(kwargs),
                route_template= tool.route
            )
            
            try:
                # Preparar headers
                headers = tool.headers or {}
                headers.setdefault("Content-Type", "application/json")
                
                # Log headers (sem dados sensíveis)
                safe_headers = {k: "***MASKED***" if any(sensitive in k.lower() 
                              for sensitive in ['authorization', 'api', 'key', 'token']) 
                              else v for k, v in headers.items()}              
                
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
                        timeout=30.0,
                        verify=True  # Sempre verificar SSL
                    )
                else:
                    # Para POST, PUT, PATCH, usar body JSON com parâmetros restantes
                    
                    response = httpx.request(
                        method=tool.http_method.value,
                        url=url,
                        json=remaining_params if remaining_params else None,
                        headers=headers,
                        timeout=30.0,
                        verify=True  # Sempre verificar SSL
                    )
                
                response.raise_for_status()
                
                # Log de sucesso
                self.logger.info(f"Requisição HTTP bem-sucedida para: {tool.id}", 
                    tool_id= tool.id,
                    request_id= request_id,
                    status_code= response.status_code,
                    response_size_bytes= len(response.content),
                    content_type= response.headers.get("content-type", "unknown")
                )
                
                # Tentar retornar JSON, senão retornar texto
                try:
                    json_response = response.json()
                    return str(json_response)
                except:
                    return response.text
                    
            except httpx.RequestError as e:
                error_msg = f"Erro na requisição: {str(e)}"
                self.logger.error(f"Erro de requisição para: {tool.id}", 
                    exception= e,
                    tool_id= tool.id,
                    request_id= request_id,
                    error_type= "RequestError",
                    error_category= "network"
                )
                return error_msg
                
            except httpx.HTTPStatusError as e:
                error_msg = f"Erro HTTP {e.response.status_code}: {e.response.text}"
                self.logger.error(f"Erro HTTP para: {tool.id}",
                    exception= e,
                    tool_id= tool.id,
                    request_id= request_id,
                    status_code= e.response.status_code,
                    error_type= "HTTPStatusError",
                    error_category= "http_status",
                    response_size_bytes= len(e.response.content) if e.response.content else 0
                )
                return error_msg
                
            except Exception as e:
                error_msg = f"Erro inesperado: {str(e)}"
                self.logger.error(f"Erro inesperado para: {tool.id}",
                    exception= e,
                    tool_id= tool.id,
                    request_id= request_id,
                    error_type= e.__class__.__name__,
                    error_category= "unexpected"
                )
                return error_msg
        
        # Criar a descrição da função para o agno
        function_description = self._create_function_description(tool)
        
        # Criar toolkit do agno
        toolkit = Toolkit(name=tool.id, instructions=function_description)
        toolkit.register(
            function=http_function,
            name=tool.id,
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
