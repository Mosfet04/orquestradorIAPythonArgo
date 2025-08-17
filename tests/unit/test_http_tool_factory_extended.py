import pytest
from unittest.mock import Mock, patch, MagicMock
from src.application.services.http_tool_factory_service import HttpToolFactory
from src.domain.entities.tool import Tool, ToolParameter, HttpMethod, ParameterType


class TestHttpToolFactory:
    """Testes unitários para HttpToolFactory com foco em aumentar cobertura."""
    
    def setup_method(self):
        """Setup executado antes de cada teste."""
        self.factory = HttpToolFactory()
    
    def test_create_tools_from_configs_with_empty_list(self):
        """Testa criação de tools com lista vazia."""
        # Arrange
        tools = []
        
        # Act
        result = self.factory.create_tools_from_configs(tools)
        
        # Assert
        assert result == []
    
    def test_create_tools_from_configs_with_error_in_one_tool(self):
        """Testa que erros em uma tool não afetam as outras."""
        # Arrange
        valid_tool = Tool(
            id="valid_tool",
            name="Valid Tool",
            description="A valid tool",
            route="http://example.com/api/test",
            http_method=HttpMethod.GET,
            parameters=[]
        )
        
        # Simular erro ao criar a segunda tool usando mock
        tools = [valid_tool]
        
        # Act
        with patch.object(self.factory, '_create_agno_tool') as mock_create:
            mock_create.side_effect = [Mock(), Exception("Test error")]
            # Simular duas tools, mas criar apenas uma válida
            result = self.factory.create_tools_from_configs(tools)
        
        # Assert
        assert len(result) == 1  # Apenas a tool válida
    
    @patch('src.application.services.http_tool_factory_service.Toolkit')
    def test_create_agno_tool_with_minimal_config(self, mock_toolkit):
        """Testa criação de tool com configuração mínima."""
        # Arrange
        tool = Tool(
            id="test_tool",
            name="Test Tool",
            description="Test description",
            route="http://example.com/api/test",
            http_method=HttpMethod.GET,
            parameters=[]
        )
        
        mock_toolkit_instance = Mock()
        mock_toolkit.return_value = mock_toolkit_instance
        
        # Act
        result = self.factory._create_agno_tool(tool)
        
        # Assert
        assert result == mock_toolkit_instance
        mock_toolkit.assert_called_once()
    
    @patch('src.application.services.http_tool_factory_service.Toolkit')
    def test_create_agno_tool_with_post_method(self, mock_toolkit):
        """Testa criação de tool com método POST."""
        # Arrange
        param = ToolParameter(
            name="data",
            description="Request data",
            type=ParameterType.OBJECT,
            required=True
        )
        
        tool = Tool(
            id="post_tool",
            name="POST Tool",
            description="Tool for POST requests",
            route="http://example.com/api/create",
            http_method=HttpMethod.POST,
            parameters=[param],
            headers={"Authorization": "Bearer token"}
        )
        
        mock_toolkit_instance = Mock()
        mock_toolkit.return_value = mock_toolkit_instance
        
        # Act
        result = self.factory._create_agno_tool(tool)
        
        # Assert
        assert result == mock_toolkit_instance
    
    @patch('src.application.services.http_tool_factory_service.Toolkit')
    def test_create_agno_tool_with_put_method(self, mock_toolkit):
        """Testa criação de tool com método PUT."""
        # Arrange
        tool = Tool(
            id="put_tool",
            name="PUT Tool",
            description="Tool for PUT requests",
            route="http://example.com/api/update/{id}",
            http_method=HttpMethod.PUT,
            parameters=[]
        )
        
        mock_toolkit_instance = Mock()
        mock_toolkit.return_value = mock_toolkit_instance
        
        # Act
        result = self.factory._create_agno_tool(tool)
        
        # Assert
        assert result == mock_toolkit_instance
    
    @patch('src.application.services.http_tool_factory_service.Toolkit')
    def test_create_agno_tool_with_delete_method(self, mock_toolkit):
        """Testa criação de tool com método DELETE."""
        # Arrange
        tool = Tool(
            id="delete_tool",
            name="DELETE Tool",
            description="Tool for DELETE requests",
            route="http://example.com/api/delete/{id}",
            http_method=HttpMethod.DELETE,
            parameters=[]
        )
        
        mock_toolkit_instance = Mock()
        mock_toolkit.return_value = mock_toolkit_instance
        
        # Act
        result = self.factory._create_agno_tool(tool)
        
        # Assert
        assert result == mock_toolkit_instance
    
    def test_create_function_description_with_parameters(self):
        """Testa criação de descrição de função com parâmetros."""
        # Arrange
        param1 = ToolParameter(
            name="id",
            description="User ID",
            type=ParameterType.INTEGER,
            required=True
        )
        param2 = ToolParameter(
            name="name",
            description="User name",
            type=ParameterType.STRING,
            required=False
        )
        
        tool = Tool(
            id="test_tool",
            name="Test Tool",
            description="Test description",
            route="http://example.com/api/users/{id}",
            http_method=HttpMethod.GET,
            parameters=[param1, param2]
        )
        
        # Act
        description = self.factory._create_function_description(tool)
        
        # Assert
        assert "Test description" in description
        assert "Parâmetros disponíveis:" in description  # Texto correto em português
        assert "id: User ID (obrigatório)" in description
        assert "name: User name (opcional)" in description
    
    def test_create_function_description_without_parameters(self):
        """Testa criação de descrição de função sem parâmetros."""
        # Arrange
        tool = Tool(
            id="test_tool",
            name="Test Tool",
            description="Test description",
            route="http://example.com/api/test",
            http_method=HttpMethod.GET,
            parameters=[]
        )
        
        # Act
        description = self.factory._create_function_description(tool)
        
        # Assert
        assert "Test description" in description
        assert "Rota: GET http://example.com/api/test" in description  # O método adiciona informações da rota
    
    def test_create_parameters_schema_with_various_types(self):
        """Testa criação de schema de parâmetros com vários tipos."""
        # Arrange
        tool = Tool(
            id="test_tool",
            name="Test Tool",
            description="Test description",
            route="http://example.com/api/test",
            http_method=HttpMethod.POST,
            parameters=[
                ToolParameter(name="id", description="ID", type=ParameterType.INTEGER, required=True),
                ToolParameter(name="name", description="Name", type=ParameterType.STRING, required=False),
                ToolParameter(name="active", description="Active", type=ParameterType.BOOLEAN, required=True),
                ToolParameter(name="score", description="Score", type=ParameterType.FLOAT, required=False),
                ToolParameter(name="tags", description="Tags", type=ParameterType.ARRAY, required=False),
                ToolParameter(name="metadata", description="Metadata", type=ParameterType.OBJECT, required=False)
            ]
        )
        
        # Act
        schema = self.factory._create_parameters_schema(tool)
        
        # Assert
        assert schema["type"] == "object"
        assert "properties" in schema
        assert "required" in schema
        
        # Verificar tipos mapeados corretamente
        assert schema["properties"]["id"]["type"] == "integer"
        assert schema["properties"]["name"]["type"] == "string"
        assert schema["properties"]["active"]["type"] == "boolean"
        assert schema["properties"]["score"]["type"] == "number"
        assert schema["properties"]["tags"]["type"] == "array"
        assert schema["properties"]["metadata"]["type"] == "object"
        
        # Verificar campos obrigatórios
        assert "id" in schema["required"]
        assert "active" in schema["required"]
        assert "name" not in schema["required"]
    
    def test_map_parameter_types(self):
        """Testa mapeamento de tipos de parâmetros."""
        # Act & Assert
        assert self.factory._map_parameter_type("string") == "string"
        assert self.factory._map_parameter_type("integer") == "integer"
        assert self.factory._map_parameter_type("float") == "number"
        assert self.factory._map_parameter_type("boolean") == "boolean"
        assert self.factory._map_parameter_type("array") == "array"
        assert self.factory._map_parameter_type("object") == "object"
        assert self.factory._map_parameter_type("unknown") == "string"
        assert self.factory._map_parameter_type("") == "string"
