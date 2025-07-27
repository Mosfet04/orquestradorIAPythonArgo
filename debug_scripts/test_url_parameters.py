"""
Script de debug para testar a funcionalidade HTTP Tool Factory
e identificar problemas com substituição de parâmetros na URL.
"""

import sys
import os
import httpx
from unittest.mock import patch, MagicMock

# Adicionar o diretório src ao path
sys.path.append(os.path.join(os.path.dirname(__file__), '..', 'src'))

from src.domain.entities.tool import Tool, HttpMethod, ToolParameter, ParameterType
from src.application.services.http_tool_factory_service import HttpToolFactory


def test_url_parameter_substitution():
    """Testa a substituição de parâmetros na URL para métodos GET."""
    
    print("🔍 Testando substituição de parâmetros na URL...")
    
    # Criar uma tool de exemplo com parâmetros
    tool = Tool(
        id="tool_001",
        name="buscar_usuario",
        description="Busca um usuário por ID",
        route="https://api.exemplo.com/usuarios/{id}",  # URL com placeholder
        http_method=HttpMethod.GET,
        parameters=[
            ToolParameter(
                name="id",
                type=ParameterType.INTEGER,
                description="ID do usuário",
                required=True
            ),
            ToolParameter(
                name="incluir_detalhes",
                type=ParameterType.BOOLEAN,
                description="Se deve incluir detalhes",
                required=False,
                default_value=False
            )
        ]
    )
    
    # Criar factory
    factory = HttpToolFactory()
    
    # Mock do httpx.request para capturar a URL gerada
    with patch('httpx.request') as mock_request:
        # Configurar mock para retornar uma resposta válida
        mock_response = MagicMock()
        mock_response.json.return_value = {"id": 123, "nome": "João"}
        mock_response.text = '{"id": 123, "nome": "João"}'
        mock_response.raise_for_status.return_value = None
        mock_request.return_value = mock_response
        
        # Criar a tool agno
        agno_tool = factory._create_agno_tool(tool)
        
        # Executar a função com parâmetros
        kwargs = {"id": 123, "incluir_detalhes": True}
        
        # Simular chamada da função HTTP diretamente
        # Vamos simular a execução criando nossa própria função
        def test_http_function(**test_kwargs):
            """Função de teste que simula a execução da tool."""
            # Preparar headers
            headers = tool.headers or {}
            headers.setdefault("Content-Type", "application/json")
            
            # Preparar dados baseado no método HTTP
            if tool.http_method in [HttpMethod.GET, HttpMethod.DELETE]:
                # Para GET e DELETE, usar params na URL
                mock_request(
                    method=tool.http_method.value,
                    url=tool.route,
                    params=test_kwargs,
                    headers=headers,
                    timeout=30.0
                )
            else:
                # Para POST, PUT, PATCH, usar body JSON
                mock_request(
                    method=tool.http_method.value,
                    url=tool.route,
                    json=test_kwargs,
                    headers=headers,
                    timeout=30.0
                )
            return str(mock_response.json())
        
        result = test_http_function(**kwargs)
        
        # Verificar o que foi chamado
        print(f"📞 httpx.request foi chamado com:")
        call_args = mock_request.call_args
        print(f"   method: {call_args.kwargs.get('method')}")
        print(f"   url: {call_args.kwargs.get('url')}")
        print(f"   params: {call_args.kwargs.get('params')}")
        print(f"   json: {call_args.kwargs.get('json')}")
        
        print(f"📋 Resultado: {result}")
        
        # PROBLEMA IDENTIFICADO:
        # A URL não está sendo processada para substituir placeholders como {id}
        # Os parâmetros estão sendo enviados como query parameters, não substituindo na URL
        
        print("\n❌ PROBLEMA IDENTIFICADO:")
        print("   A URL não está substituindo placeholders como {id}")
        print("   Os parâmetros estão sendo enviados como query parameters")
        print("   Para URLs como '/usuarios/{id}', o {id} deveria ser substituído")


def test_improved_url_handling():
    """Testa uma versão melhorada do tratamento de URL."""
    
    print("\n🔧 Testando versão melhorada do tratamento de URL...")
    
    def improved_http_function(tool: Tool, **kwargs) -> str:
        """Versão melhorada que trata placeholders na URL."""
        try:
            # Preparar headers
            headers = tool.headers or {}
            headers.setdefault("Content-Type", "application/json")
            
            # Processar URL para substituir placeholders
            url = tool.route
            url_params = {}
            query_params = {}
            
            # Separar parâmetros de URL de query parameters
            for key, value in kwargs.items():
                placeholder = f"{{{key}}}"
                if placeholder in url:
                    # Este parâmetro deve ser substituído na URL
                    url = url.replace(placeholder, str(value))
                    url_params[key] = value
                else:
                    # Este parâmetro deve ir como query parameter
                    query_params[key] = value
            
            print(f"🔄 URL original: {tool.route}")
            print(f"🔄 URL processada: {url}")
            print(f"🔄 Parâmetros de URL: {url_params}")
            print(f"🔄 Query parameters: {query_params}")
            
            # Preparar dados baseado no método HTTP
            if tool.http_method in [HttpMethod.GET, HttpMethod.DELETE]:
                # Para GET e DELETE, usar apenas query params restantes
                response_data = {
                    "method": tool.http_method.value,
                    "url": url,
                    "params": query_params if query_params else None,
                    "headers": headers,
                    "timeout": 30.0
                }
            else:
                # Para POST, PUT, PATCH, usar body JSON
                response_data = {
                    "method": tool.http_method.value,
                    "url": url,
                    "json": query_params if query_params else None,
                    "headers": headers,
                    "timeout": 30.0
                }
            
            return f"✅ Requisição preparada: {response_data}"
            
        except Exception as e:
            return f"❌ Erro: {str(e)}"
    
    # Teste com URL que tem placeholder
    tool_with_placeholder = Tool(
        id="tool_001",
        name="buscar_usuario",
        description="Busca um usuário por ID",
        route="https://api.exemplo.com/usuarios/{id}",
        http_method=HttpMethod.GET,
        parameters=[
            ToolParameter(
                name="id",
                type=ParameterType.INTEGER,
                description="ID do usuário",
                required=True
            ),
            ToolParameter(
                name="incluir_detalhes",
                type=ParameterType.BOOLEAN,
                description="Se deve incluir detalhes",
                required=False
            )
        ]
    )
    
    result = improved_http_function(tool_with_placeholder, id=123, incluir_detalhes=True)
    print(f"📋 Resultado com placeholder: {result}")
    
    # Teste com URL sem placeholder
    tool_without_placeholder = Tool(
        id="tool_002",
        name="listar_usuarios",
        description="Lista usuários",
        route="https://api.exemplo.com/usuarios",
        http_method=HttpMethod.GET,
        parameters=[
            ToolParameter(
                name="limite",
                type=ParameterType.INTEGER,
                description="Limite de resultados",
                required=False
            )
        ]
    )
    
    result = improved_http_function(tool_without_placeholder, limite=10)
    print(f"📋 Resultado sem placeholder: {result}")


if __name__ == "__main__":
    print("🐛 Debug: HTTP Tool Factory - Substituição de Parâmetros na URL")
    print("=" * 60)
    
    test_url_parameter_substitution()
    test_improved_url_handling()
    
    print("\n💡 SOLUÇÃO RECOMENDADA:")
    print("   1. Modificar a função http_function para detectar placeholders na URL")
    print("   2. Substituir placeholders como {id} pelos valores dos parâmetros")
    print("   3. Separar parâmetros de URL de query parameters")
    print("   4. Usar apenas query parameters restantes para métodos GET/DELETE")
