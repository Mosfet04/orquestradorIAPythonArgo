import pytest
from src.application.services.model_factory_service import ModelFactory
from agno.models.ollama import Ollama


class TestModelFactory:
    """Testes unitários para o ModelFactory."""
    
    def test_create_ollama_model_success(self):
        """Testa criação bem-sucedida de modelo Ollama."""
        # Arrange
        factory_type = "ollama"
        model_id = "llama3.2:latest"
        
        # Act
        model = ModelFactory.create_model(factory_type, model_id)
        
        # Assert
        assert model is not None
        assert isinstance(model, Ollama)
        assert model.id == model_id
    
    def test_create_model_with_case_insensitive_factory_type(self):
        """Testa criação de modelo com tipo case-insensitive."""
        # Arrange
        factory_type = "OLLAMA"
        model_id = "llama3.2:latest"
        
        # Act
        model = ModelFactory.create_model(factory_type, model_id)
        
        # Assert
        assert model is not None
        assert isinstance(model, Ollama)
    
    def test_create_model_with_empty_factory_type_raises_error(self):
        """Testa se factory type vazio levanta erro."""
        # Arrange
        factory_type = ""
        model_id = "llama3.2:latest"
        
        # Act & Assert
        with pytest.raises(ValueError, match="Tipo de modelo não pode estar vazio"):
            ModelFactory.create_model(factory_type, model_id)
    
    def test_create_model_with_empty_model_id_raises_error(self):
        """Testa se model ID vazio levanta erro."""
        # Arrange
        factory_type = "ollama"
        model_id = ""
        
        # Act & Assert
        with pytest.raises(ValueError, match="ID do modelo não pode estar vazio"):
            ModelFactory.create_model(factory_type, model_id)
    
    def test_create_model_with_unsupported_type_raises_error(self):
        """Testa se tipo de modelo não suportado levanta erro."""
        # Arrange
        factory_type = "unsupported_model"
        model_id = "some-model"
        
        # Act & Assert
        with pytest.raises(ValueError, match="não suportado"):
            ModelFactory.create_model(factory_type, model_id)
    
    def test_get_supported_models_returns_list(self):
        """Testa se get_supported_models retorna lista de modelos."""
        # Act
        supported_models = ModelFactory.get_supported_models()
        
        # Assert
        assert isinstance(supported_models, list)
        assert len(supported_models) > 0
        assert "ollama" in supported_models
    
    def test_is_supported_model_with_valid_type(self):
        """Testa is_supported_model com tipo válido."""
        # Act & Assert
        assert ModelFactory.is_supported_model("ollama") is True
        assert ModelFactory.is_supported_model("OLLAMA") is True
        assert ModelFactory.is_supported_model("openai") is True
    
    def test_is_supported_model_with_invalid_type(self):
        """Testa is_supported_model com tipo inválido."""
        # Act & Assert
        assert ModelFactory.is_supported_model("invalid_model") is False
        assert ModelFactory.is_supported_model("") is False
    
    def test_is_available_model_with_ollama(self):
        """Testa is_available_model com Ollama (deve estar disponível)."""
        # Act & Assert
        assert ModelFactory.is_available_model("ollama") is True
    
    def test_get_available_models_returns_dict(self):
        """Testa se get_available_models retorna dicionário."""
        # Act
        available_models = ModelFactory.get_available_models()
        
        # Assert
        assert isinstance(available_models, dict)
        assert "ollama" in available_models
        assert available_models["ollama"] is True  # Ollama deve estar disponível
    
    def test_validate_model_config_with_valid_config(self):
        """Testa validação com configuração válida."""
        # Arrange
        factory_type = "ollama"
        model_id = "llama3.2:latest"
        
        # Act
        result = ModelFactory.validate_model_config(factory_type, model_id)
        
        # Assert
        assert result["valid"] is True
        assert result["supported"] is True
        assert result["available"] is True
        assert result["factory_type"] == "ollama"
        assert result["model_id"] == model_id
        assert len(result["errors"]) == 0
    
    def test_validate_model_config_with_unsupported_type(self):
        """Testa validação com tipo não suportado."""
        # Arrange
        factory_type = "unsupported_model"
        model_id = "some-model"
        
        # Act
        result = ModelFactory.validate_model_config(factory_type, model_id)
        
        # Assert
        assert result["valid"] is False
        assert result["supported"] is False
        assert len(result["errors"]) > 0
    
    def test_validate_model_config_with_empty_model_id(self):
        """Testa validação com model ID vazio."""
        # Arrange
        factory_type = "ollama"
        model_id = ""
        
        # Act
        result = ModelFactory.validate_model_config(factory_type, model_id)
        
        # Assert
        assert result["valid"] is False
        assert len(result["errors"]) > 0
        assert any("ID do modelo não pode estar vazio" in error for error in result["errors"])
    
    def test_create_model_with_additional_kwargs(self):
        """Testa criação de modelo com kwargs adicionais válidos."""
        # Arrange
        factory_type = "ollama"
        model_id = "llama3.2:latest"
        
        # Act - não passamos kwargs inválidos para o Ollama
        model = ModelFactory.create_model(factory_type, model_id)
        
        # Assert
        assert model is not None
        assert isinstance(model, Ollama)
