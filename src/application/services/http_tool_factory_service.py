import httpx
from typing import List, Dict, Any, Tuple
import ast
from agno.tools import Toolkit
from src.domain.entities.tool import Tool, HttpMethod
from src.infrastructure.logging import (
    LoggerFactory, 
    log_execution, 
    log_http_request,
    log_performance
)


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
                headers = self._default_headers(tool)
                url, remaining_params = self._resolve_url_and_params(tool.route, kwargs)
                response = self._send_request(tool.http_method, url, headers, remaining_params)
                response.raise_for_status()
                self._log_success(tool.id, request_id, response)
                return self._serialize_response(response)
            except Exception as e:  # Centraliza tratamento para reduzir complexidade
                return self._handle_exception(tool, request_id, e)
        
        # Criar a descrição da função para o agno
        function_description = self._create_function_description(tool)
        
        # Criar toolkit do agno
        toolkit = Toolkit(name=tool.id, instructions=function_description)
        toolkit.register(
            function=http_function,
            name=tool.id,
        )
        return toolkit

    def _default_headers(self, tool: Tool) -> Dict[str, str]:
        """Monta headers padrão garantindo Content-Type JSON."""
        headers = (tool.headers or {}).copy()
        headers.setdefault("Content-Type", "application/json")
        return headers

    def _resolve_url_and_params(self, route: str, kwargs: Dict[str, Any]) -> Tuple[str, Dict[str, Any]]:
        """Substitui placeholders na rota e retorna URL final e parâmetros remanescentes."""
        url = route
        remaining_params = kwargs.copy()

        for key, value in kwargs.items():
            try:
                param_dict = ast.literal_eval(value) if isinstance(value, str) else value
            except (ValueError, SyntaxError):
                param_dict = value

            if isinstance(param_dict, dict) and param_dict:
                param_key = next(iter(param_dict))
                param_value = param_dict[param_key]
                placeholder = f"{{{param_key}}}"
                if placeholder in url:
                    url = url.replace(placeholder, str(param_value))
                    # remover o argumento original da lista de params
                    remaining_params.pop(key, None)

        return url, remaining_params

    def _send_request(self, http_method: HttpMethod, url: str, headers: Dict[str, str], remaining_params: Dict[str, Any]) -> httpx.Response:
        """Dispara a requisição HTTP conforme o método."""
        is_param_method = http_method in [HttpMethod.GET, HttpMethod.DELETE]
        request_kwargs: Dict[str, Any] = {
            "method": http_method.value,
            "url": url,
            "headers": headers,
            "timeout": 30.0,
            "verify": True,
        }

        if is_param_method:
            request_kwargs["params"] = remaining_params or None
        else:
            request_kwargs["json"] = remaining_params or None

        return httpx.request(**request_kwargs)

    def _log_success(self, tool_id: str, request_id: str, response: httpx.Response) -> None:
        """Loga informações de sucesso da requisição."""
        self.logger.info(
            f"Requisição HTTP bem-sucedida para: {tool_id}",
            tool_id=tool_id,
            request_id=request_id,
            status_code=response.status_code,
            response_size_bytes=len(response.content),
            content_type=response.headers.get("content-type", "unknown"),
        )

    def _serialize_response(self, response: httpx.Response) -> str:
        """Retorna JSON como string, senão texto bruto."""
        try:
            json_response = response.json()
            return str(json_response)
        except (ValueError, httpx.DecodingError):
            return response.text

    def _handle_exception(self, tool: Tool, request_id: str, e: Exception) -> str:
        """Centraliza tratamento e logging de exceções, retornando mensagem ao chamador."""
        if isinstance(e, httpx.RequestError):
            self.logger.error(
                f"Erro de requisição para: {tool.id}",
                exception=e,
                tool_id=tool.id,
                request_id=request_id,
                error_type="RequestError",
                error_category="network",
            )
            return f"Erro na requisição: {str(e)}"

        if isinstance(e, httpx.HTTPStatusError):
            resp = e.response
            self.logger.error(
                f"Erro HTTP para: {tool.id}",
                exception=e,
                tool_id=tool.id,
                request_id=request_id,
                status_code=resp.status_code if resp is not None else None,
                error_type="HTTPStatusError",
                error_category="http_status",
                response_size_bytes=len(resp.content) if getattr(resp, "content", None) else 0,
            )
            status_code = resp.status_code if resp is not None else "?"
            text = resp.text if resp is not None else ""
            return f"Erro HTTP {status_code}: {text}"

        self.logger.error(
            f"Erro inesperado para: {tool.id}",
            exception=e,
            tool_id=tool.id,
            request_id=request_id,
            error_type=e.__class__.__name__,
            error_category="unexpected",
        )
        return f"Erro inesperado: {str(e)}"
    
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
