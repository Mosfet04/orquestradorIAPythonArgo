import pytest
from unittest.mock import Mock, MagicMock, patch, AsyncMock
from src.infrastructure.repositories.mongo_tool_repository import MongoToolRepository
from src.domain.entities.tool import Tool, ParameterType, HttpMethod, ToolParameter


class TestMongoToolRepositoryExtended:
    """Testes unitários adicionais para MongoToolRepository com foco em cobertura."""
    
    def setup_method(self):
        """Setup executado antes de cada teste."""
        with patch('src.infrastructure.repositories.mongo_tool_repository.MongoClient') as mock_client:
            # Usar MagicMock para suportar __getitem__ (método mágico) no mock do DB
            mock_db = MagicMock()
            mock_collection = Mock()
            mock_client.return_value.__getitem__.return_value = mock_db
            mock_db.__getitem__.return_value = mock_collection
            mock_client.return_value.admin.command = Mock(return_value={})  # Mock ping command
            
            self.repository = MongoToolRepository(
                connection_string="mongodb://localhost:62659/?directConnection=true",
                database_name="test_db"
            )
            self.mock_collection = mock_collection
    
    def test_repository_initialization(self):
        """Testa inicialização do repositório."""
        # Assert
        assert self.repository._connection_string == "mongodb://localhost:62659/?directConnection=true"
        assert self.repository._database_name == "test_db"
        assert self.repository._collection_name == "tools"
    
    def test_get_all_active_tools_success(self):
        """Testa busca de todas as ferramentas ativas com sucesso."""
        # Arrange
        mock_tool_data = [
            {
                "id": "tool1",
                "name": "Tool 1",
                "description": "Description 1",
                "route": "http://api.test.com/tool1",
                "http_method": "GET",
                "parameters": [],
                "active": True
            },
            {
                "id": "tool2",
                "name": "Tool 2",
                "description": "Description 2",
                "route": "http://api.test.com/tool2",
                "http_method": "POST",
                "parameters": [
                    {"name": "param1", "type": "STRING", "required": True, "description": "Parameter 1"}
                ],
                "active": True
            }
        ]
        
        with patch.object(self.repository, '_get_collection') as mock_get_collection:
            mock_get_collection.return_value.find.return_value = mock_tool_data
            
            # Act
            result = self.repository.get_all_active_tools()
            
            # Assert
            assert len(result) == 2
            assert all(isinstance(tool, Tool) for tool in result)
            assert result[0].id == "tool1"
            assert result[0].name == "Tool 1"
            assert result[1].id == "tool2"
            assert result[1].name == "Tool 2"
            assert len(result[1].parameters) == 1
    
    def test_get_all_active_tools_empty_result(self):
        """Testa busca de todas as ferramentas ativas com resultado vazio."""
        # Arrange
        with patch.object(self.repository, '_get_collection') as mock_get_collection:
            mock_get_collection.return_value.find.return_value = []
            
            # Act
            result = self.repository.get_all_active_tools()
            
            # Assert
            assert result == []
    
    def test_get_tools_by_ids_success(self):
        """Testa busca de ferramentas por IDs com sucesso."""
        # Arrange
        tool_ids = ["tool1", "tool2"]
        mock_tool_data = [
            {
                "id": "tool1",
                "name": "Tool 1",
                "description": "Description 1",
                "route": "http://api.test.com/tool1",
                "http_method": "GET",
                "parameters": [],
                "active": True
            }
        ]
        
        with patch.object(self.repository, '_get_collection') as mock_get_collection:
            mock_get_collection.return_value.find.return_value = mock_tool_data
            
            # Act
            result = self.repository.get_tools_by_ids(tool_ids)
            
            # Assert
            assert len(result) == 1
            assert result[0].id == "tool1"
            mock_get_collection.return_value.find.assert_called_once_with({"id": {"$in": tool_ids}, "active": True})
    
    def test_get_tools_by_ids_empty_list(self):
        """Testa busca de ferramentas por IDs com lista vazia."""
        # Arrange
        tool_ids = []
        
        # Act
        result = self.repository.get_tools_by_ids(tool_ids)
        
        # Assert
        assert result == []
    
    def test_get_tool_by_id_success(self):
        """Testa busca de ferramenta por ID com sucesso."""
        # Arrange
        tool_id = "tool1"
        mock_tool_data = {
            "id": "tool1",
            "name": "Tool 1",
            "description": "Description 1",
            "route": "http://api.test.com/tool1",
            "http_method": "GET",
            "parameters": [],
            "active": True
        }
        
        with patch.object(self.repository, '_get_collection') as mock_get_collection:
            mock_get_collection.return_value.find_one.return_value = mock_tool_data
            
            # Act
            result = self.repository.get_tool_by_id(tool_id)
            
            # Assert
            assert result is not None
            assert result.id == "tool1"
            assert result.name == "Tool 1"
            mock_get_collection.return_value.find_one.assert_called_once_with({"id": tool_id})
    
    def test_get_tool_by_id_not_found(self):
        """Testa busca de ferramenta por ID não encontrada."""
        # Arrange
        tool_id = "nonexistent_tool"
        
        with patch.object(self.repository, '_get_collection') as mock_get_collection:
            mock_get_collection.return_value.find_one.return_value = None
            
            # Act & Assert
            with pytest.raises(ValueError, match=f"Tool com ID {tool_id} não encontrada"):
                self.repository.get_tool_by_id(tool_id)
    
    def test_map_to_entity_conversion_with_all_fields(self):
        """Testa conversão de dicionário para Tool com todos os campos."""
        # Arrange
        tool_dict = {
            "id": "test_tool",
            "name": "Test Tool",
            "description": "Test Description",
            "route": "http://api.test.com/tool",
            "http_method": "POST",
            "parameters": [
                {
                    "name": "param1",
                    "type": "STRING",
                    "required": True,
                    "description": "Parameter 1",
                    "default_value": "default"
                },
                {
                    "name": "param2",
                    "type": "INTEGER",
                    "required": False,
                    "description": "Parameter 2"
                }
            ],
            "instructions": "Test instructions",
            "headers": {"Content-Type": "application/json"},
            "active": True
        }
        
        # Act
        result = self.repository._map_to_entity(tool_dict)
        
        # Assert
        assert result.id == "test_tool"
        assert result.name == "Test Tool"
        assert result.description == "Test Description"
        assert result.route == "http://api.test.com/tool"
        assert result.http_method == HttpMethod.POST
        assert len(result.parameters) == 2
        assert result.parameters[0].type == ParameterType.STRING
        assert result.parameters[1].type == ParameterType.INTEGER
        assert result.instructions == "Test instructions"
        assert result.headers == {"Content-Type": "application/json"}
        assert result.active == True
    
    def test_map_to_entity_conversion_with_minimal_fields(self):
        """Testa conversão de dicionário para Tool com campos mínimos."""
        # Arrange
        tool_dict = {
            "id": "minimal_tool",
            "name": "Minimal Tool",
            "description": "Minimal Description",
            "route": "http://api.test.com/minimal",
            "http_method": "GET"
        }
        
        # Act
        result = self.repository._map_to_entity(tool_dict)
        
        # Assert
        assert result.id == "minimal_tool"
        assert result.name == "Minimal Tool"
        assert result.parameters == []
        assert result.instructions == ""
        assert result.headers == {}
        assert result.active == True  # default value
    
    def test_should_use_tls_with_atlas(self):
        """Testa detecção de TLS para MongoDB Atlas."""
        # Arrange
        with patch('src.infrastructure.repositories.mongo_tool_repository.MongoClient'):
            atlas_repo = MongoToolRepository(
                connection_string="mongodb+srv://user:pass@cluster.mongodb.net/db"
            )
            
            # Act
            result = atlas_repo._should_use_tls()
            
            # Assert
            assert result == True
    
    def test_should_use_tls_with_local_mongodb(self):
        """Testa detecção de TLS para MongoDB local."""
        # Arrange
        with patch('os.getenv', return_value="false"):
            with patch('src.infrastructure.repositories.mongo_tool_repository.MongoClient'):
                local_repo = MongoToolRepository(
                    connection_string="mongodb://localhost:62659/?directConnection=true"
                )
                
                # Act
                result = local_repo._should_use_tls()
                
                # Assert
                assert result == False
    
    def test_get_collection_with_connection_error(self):
        """Testa _get_collection com erro de conexão."""
        # Arrange
        with patch('src.infrastructure.repositories.mongo_tool_repository.MongoClient') as mock_client:
            from pymongo.errors import ConnectionFailure
            mock_client.side_effect = ConnectionFailure("Connection failed")
            
            repo = MongoToolRepository()
            
            # Act & Assert
            with pytest.raises(ConnectionError, match="Não foi possível conectar ao MongoDB"):
                repo._get_collection()
    
    def test_get_tools_by_ids_with_database_error(self):
        """Testa get_tools_by_ids com erro de banco de dados."""
        # Arrange
        with patch.object(self.repository, '_get_collection') as mock_get_collection:
            mock_get_collection.side_effect = Exception("Database error")
            
            # Act & Assert
            with pytest.raises(Exception, match="Database error"):
                self.repository.get_tools_by_ids(["tool1", "tool2"])
    
    def test_get_tool_by_id_with_database_error(self):
        """Testa get_tool_by_id com erro de banco de dados."""
        # Arrange
        with patch.object(self.repository, '_get_collection') as mock_get_collection:
            mock_get_collection.side_effect = Exception("Database error")
            
            # Act & Assert
            with pytest.raises(Exception, match="Database error"):
                self.repository.get_tool_by_id("tool1")
    
    def test_parameter_type_mapping_all_types(self):
        """Testa mapeamento de todos os tipos de parâmetros."""
        # Arrange
        test_cases = [
            ("STRING", ParameterType.STRING),
            ("INTEGER", ParameterType.INTEGER),
            ("FLOAT", ParameterType.FLOAT),
            ("BOOLEAN", ParameterType.BOOLEAN),
            ("OBJECT", ParameterType.OBJECT),
            ("ARRAY", ParameterType.ARRAY)
        ]
        
        for tipo_str, expected_type in test_cases:
            tool_dict = {
                "id": f"tool_{tipo_str.lower()}",
                "name": f"Tool {tipo_str}",
                "description": "Test Description",
                "route": "http://api.test.com/tool",
                "http_method": "GET",
                "parameters": [
                    {
                        "name": "param",
                        "type": tipo_str,
                        "required": True,
                        "description": "Test parameter"
                    }
                ]
            }
            
            # Act
            result = self.repository._map_to_entity(tool_dict)
            
            # Assert
            assert result.parameters[0].type == expected_type
    
    def test_http_method_mapping(self):
        """Testa mapeamento de métodos HTTP."""
        # Arrange
        test_cases = [
            ("GET", HttpMethod.GET),
            ("POST", HttpMethod.POST),
            ("PUT", HttpMethod.PUT),
            ("DELETE", HttpMethod.DELETE),
            ("PATCH", HttpMethod.PATCH)
        ]
        
        for method_str, expected_method in test_cases:
            tool_dict = {
                "id": f"tool_{method_str.lower()}",
                "name": f"Tool {method_str}",
                "description": "Test Description",
                "route": "http://api.test.com/tool",
                "http_method": method_str,
                "parameters": []
            }
            
            # Act
            result = self.repository._map_to_entity(tool_dict)
            
            # Assert
            assert result.http_method == expected_method
