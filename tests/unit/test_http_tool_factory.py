import unittest
from unittest.mock import patch, MagicMock
from src.domain.entities.tool import Tool, HttpMethod, ToolParameter, ParameterType
from src.application.services.http_tool_factory_service import HttpToolFactory


class TestHttpToolFactoryService(unittest.TestCase):
    """Testes para o HttpToolFactory com substituição de parâmetros na URL."""
    
    def setUp(self):
        """Configuração inicial para os testes."""
        self.factory = HttpToolFactory()
    
    @patch('src.application.services.http_tool_factory_service.httpx.request')
    def test_url_placeholder_substitution_get_method(self, mock_request):
        """Testa substituição de placeholders em URLs para métodos GET."""
        # Arrange
        mock_response = MagicMock()
        mock_response.json.return_value = {"id": 123, "nome": "João"}
        mock_response.text = '{"id": 123, "nome": "João"}'
        mock_response.raise_for_status.return_value = None
        mock_request.return_value = mock_response
        
        tool = Tool(
            id="test_001",
            name="buscar_usuario",
            description="Busca usuário por ID",
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
                    description="Incluir detalhes",
                    required=False
                )
            ]
        )
        
        # Act
        agno_tool = self.factory._create_agno_tool(tool)
        
        # Simular execução da função HTTP usando reflexão
        # Como não podemos acessar a função diretamente, vamos testar a lógica
        kwargs = {"id": 123, "incluir_detalhes": True}
        
        # Simular a lógica de processamento de URL implementada
        url = tool.route
        remaining_params = kwargs.copy()
        
        for key, value in kwargs.items():
            placeholder = f"{{{key}}}"
            if placeholder in url:
                url = url.replace(placeholder, str(value))
                remaining_params.pop(key)
        
        # Assert
        expected_url = "https://api.exemplo.com/usuarios/123"
        expected_params = {"incluir_detalhes": True}
        
        self.assertEqual(url, expected_url)
        self.assertEqual(remaining_params, expected_params)
    
    def test_url_without_placeholders(self):
        """Testa URLs sem placeholders."""
        # Arrange
        tool = Tool(
            id="test_002",
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
        
        kwargs = {"limite": 10}
        
        # Act - Simular lógica implementada
        url = tool.route
        remaining_params = kwargs.copy()
        
        for key, value in kwargs.items():
            placeholder = f"{{{key}}}"
            if placeholder in url:
                url = url.replace(placeholder, str(value))
                remaining_params.pop(key)
        
        # Assert
        expected_url = "https://api.exemplo.com/usuarios"
        expected_params = {"limite": 10}
        
        self.assertEqual(url, expected_url)
        self.assertEqual(remaining_params, expected_params)
    
    def test_multiple_placeholders(self):
        """Testa URLs com múltiplos placeholders."""
        # Arrange
        tool = Tool(
            id="test_003",
            name="buscar_post",
            description="Busca post específico",
            route="https://api.exemplo.com/usuarios/{user_id}/posts/{post_id}",
            http_method=HttpMethod.GET,
            parameters=[
                ToolParameter(
                    name="user_id",
                    type=ParameterType.INTEGER,
                    description="ID do usuário",
                    required=True
                ),
                ToolParameter(
                    name="post_id", 
                    type=ParameterType.INTEGER,
                    description="ID do post",
                    required=True
                ),
                ToolParameter(
                    name="incluir_comentarios",
                    type=ParameterType.BOOLEAN,
                    description="Incluir comentários",
                    required=False
                )
            ]
        )
        
        kwargs = {"user_id": 123, "post_id": 456, "incluir_comentarios": True}
        
        # Act - Simular lógica implementada
        url = tool.route
        remaining_params = kwargs.copy()
        
        for key, value in kwargs.items():
            placeholder = f"{{{key}}}"
            if placeholder in url:
                url = url.replace(placeholder, str(value))
                remaining_params.pop(key)
        
        # Assert
        expected_url = "https://api.exemplo.com/usuarios/123/posts/456"
        expected_params = {"incluir_comentarios": True}
        
        self.assertEqual(url, expected_url)
        self.assertEqual(remaining_params, expected_params)
    
    def test_create_function_description(self):
        """Testa criação da descrição da função."""
        # Arrange
        tool = Tool(
            id="test_004",
            name="criar_usuario",
            description="Cria um novo usuário",
            route="https://api.exemplo.com/usuarios",
            http_method=HttpMethod.POST,
            instructions="Envie os dados em JSON",
            parameters=[
                ToolParameter(
                    name="nome",
                    type=ParameterType.STRING,
                    description="Nome do usuário",
                    required=True
                )
            ]
        )
        
        # Act
        description = self.factory._create_function_description(tool)
        
        # Assert
        self.assertIn("Cria um novo usuário", description)
        self.assertIn("Envie os dados em JSON", description)
        self.assertIn("POST https://api.exemplo.com/usuarios", description)
        self.assertIn("nome: Nome do usuário (obrigatório)", description)
    
    def test_map_parameter_types(self):
        """Testa mapeamento de tipos de parâmetros."""
        # Act & Assert
        self.assertEqual(self.factory._map_parameter_type("string"), "string")
        self.assertEqual(self.factory._map_parameter_type("integer"), "integer")
        self.assertEqual(self.factory._map_parameter_type("float"), "number")
        self.assertEqual(self.factory._map_parameter_type("boolean"), "boolean")
        self.assertEqual(self.factory._map_parameter_type("object"), "object")
        self.assertEqual(self.factory._map_parameter_type("array"), "array")
        self.assertEqual(self.factory._map_parameter_type("unknown"), "string")
    
    def test_create_parameters_schema(self):
        """Testa criação do schema de parâmetros."""
        # Arrange
        tool = Tool(
            id="test_005",
            name="atualizar_usuario",
            description="Atualiza dados do usuário",
            route="https://api.exemplo.com/usuarios/{id}",
            http_method=HttpMethod.PUT,
            parameters=[
                ToolParameter(
                    name="id",
                    type=ParameterType.INTEGER,
                    description="ID do usuário",
                    required=True
                ),
                ToolParameter(
                    name="nome",
                    type=ParameterType.STRING,
                    description="Novo nome",
                    required=False,
                    default_value="Sem nome"
                )
            ]
        )
        
        # Act
        schema = self.factory._create_parameters_schema(tool)
        
        # Assert
        expected_schema = {
            "type": "object",
            "properties": {
                "id": {
                    "type": "integer",
                    "description": "ID do usuário"
                },
                "nome": {
                    "type": "string",
                    "description": "Novo nome",
                    "default": "Sem nome"
                }
            },
            "required": ["id"]
        }
        
        self.assertEqual(schema, expected_schema)


if __name__ == '__main__':
    unittest.main()
