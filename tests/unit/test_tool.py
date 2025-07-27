import pytest
from src.domain.entities.tool import Tool, ToolParameter, HttpMethod, ParameterType


class TestTool:
    """Testes unitários para a entidade Tool."""
    
    def test_create_tool_with_valid_data(self):
        """Testa criação de Tool com dados válidos."""
        # Arrange
        parameters = [
            ToolParameter(
                name="user_id",
                type=ParameterType.STRING,
                description="ID do usuário",
                required=True
            ),
            ToolParameter(
                name="limit",
                type=ParameterType.INTEGER,
                description="Limite de resultados",
                required=False,
                default_value=10
            )
        ]
        
        # Act
        tool = Tool(
            id="get-user-info",
            name="Get User Info",
            description="Busca informações do usuário",
            route="https://api.example.com/user/{user_id}",
            http_method=HttpMethod.GET,
            parameters=parameters,
            instructions="Use esta ferramenta para buscar dados do usuário",
            headers={"Authorization": "Bearer token"}
        )
        
        # Assert
        assert tool.id == "get-user-info"
        assert tool.name == "Get User Info"
        assert tool.description == "Busca informações do usuário"
        assert tool.route == "https://api.example.com/user/{user_id}"
        assert tool.http_method == HttpMethod.GET
        assert len(tool.parameters) == 2
        assert tool.instructions == "Use esta ferramenta para buscar dados do usuário"
        assert tool.headers["Authorization"] == "Bearer token"
        assert tool.active is True
    
    def test_create_tool_without_parameters(self):
        """Testa criação de Tool sem parâmetros."""
        # Act
        tool = Tool(
            id="health-check",
            name="Health Check",
            description="Verifica status da API",
            route="https://api.example.com/health",
            http_method=HttpMethod.GET,
            parameters=[]
        )
        
        # Assert
        assert tool.id == "health-check"
        assert len(tool.parameters) == 0
    
    def test_tool_with_empty_id_raises_error(self):
        """Testa se ID vazio levanta ValueError."""
        with pytest.raises(ValueError, match="ID da tool não pode estar vazio"):
            Tool(
                id="",
                name="Test Tool",
                description="Descrição",
                route="https://api.example.com",
                http_method=HttpMethod.GET,
                parameters=[]
            )
    
    def test_tool_with_empty_name_raises_error(self):
        """Testa se nome vazio levanta ValueError."""
        with pytest.raises(ValueError, match="Nome da tool não pode estar vazio"):
            Tool(
                id="test-tool",
                name="",
                description="Descrição",
                route="https://api.example.com",
                http_method=HttpMethod.GET,
                parameters=[]
            )
    
    def test_tool_with_empty_description_raises_error(self):
        """Testa se descrição vazia levanta ValueError."""
        with pytest.raises(ValueError, match="Descrição da tool não pode estar vazia"):
            Tool(
                id="test-tool",
                name="Test Tool",
                description="",
                route="https://api.example.com",
                http_method=HttpMethod.GET,
                parameters=[]
            )
    
    def test_tool_with_empty_route_raises_error(self):
        """Testa se rota vazia levanta ValueError."""
        with pytest.raises(ValueError, match="Rota da tool não pode estar vazia"):
            Tool(
                id="test-tool",
                name="Test Tool",
                description="Descrição",
                route="",
                http_method=HttpMethod.GET,
                parameters=[]
            )


class TestToolParameter:
    """Testes unitários para a entidade ToolParameter."""
    
    def test_create_parameter_with_valid_data(self):
        """Testa criação de ToolParameter com dados válidos."""
        # Act
        param = ToolParameter(
            name="user_id",
            type=ParameterType.STRING,
            description="ID do usuário",
            required=True,
            default_value="guest"
        )
        
        # Assert
        assert param.name == "user_id"
        assert param.type == ParameterType.STRING
        assert param.description == "ID do usuário"
        assert param.required is True
        assert param.default_value == "guest"
    
    def test_create_parameter_with_defaults(self):
        """Testa criação de ToolParameter com valores padrão."""
        # Act
        param = ToolParameter(
            name="limit",
            type=ParameterType.INTEGER,
            description="Limite de resultados"
        )
        
        # Assert
        assert param.required is False
        assert param.default_value is None
    
    def test_parameter_with_empty_name_raises_error(self):
        """Testa se nome vazio levanta ValueError."""
        with pytest.raises(ValueError, match="Nome do parâmetro não pode estar vazio"):
            ToolParameter(
                name="",
                type=ParameterType.STRING,
                description="Descrição"
            )
    
    def test_parameter_with_empty_description_raises_error(self):
        """Testa se descrição vazia levanta ValueError."""
        with pytest.raises(ValueError, match="Descrição do parâmetro não pode estar vazia"):
            ToolParameter(
                name="param",
                type=ParameterType.STRING,
                description=""
            )
