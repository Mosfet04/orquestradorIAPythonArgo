import unittest
from unittest.mock import patch, MagicMock
import os
from src.application.services.embedder_model_factory_service import EmbedderModelFactory


class TestEmbedderModelFactory(unittest.TestCase):
    """Testes unitários para o EmbedderModelFactory."""
    
    def setUp(self):
        """Configuração inicial para os testes."""
        self.factory = EmbedderModelFactory()
    
    def test_get_supported_models_returns_list(self):
        """Testa se get_supported_models retorna uma lista."""
        result = self.factory.get_supported_models()
        self.assertIsInstance(result, list)
        self.assertGreater(len(result), 0)
        self.assertIn("ollama", result)
        self.assertIn("openai", result)
        self.assertIn("gemini", result)
    
    def test_get_available_models_returns_dict(self):
        """Testa se get_available_models retorna um dicionário."""
        result = self.factory.get_available_models()
        self.assertIsInstance(result, dict)
        self.assertGreater(len(result), 0)
        
        # Verificar se todas as chaves são dos modelos suportados
        supported = self.factory.get_supported_models()
        for model in result.keys():
            self.assertIn(model, supported)
    
    def test_is_supported_model_with_valid_type(self):
        """Testa is_supported_model com tipos válidos."""
        self.assertTrue(self.factory.is_supported_model("ollama"))
        self.assertTrue(self.factory.is_supported_model("openai"))
        self.assertTrue(self.factory.is_supported_model("OLLAMA"))  # Case insensitive
    
    def test_is_supported_model_with_invalid_type(self):
        """Testa is_supported_model com tipos inválidos."""
        self.assertFalse(self.factory.is_supported_model("invalid"))
        self.assertFalse(self.factory.is_supported_model(""))
        self.assertFalse(self.factory.is_supported_model("random_model"))
    
    def test_validate_model_config_with_valid_config(self):
        """Testa validate_model_config com configuração válida."""
        result = self.factory.validate_model_config("ollama", "nomic-embed-text")
        
        self.assertIsInstance(result, dict)
        self.assertIn("valid", result)
        self.assertIn("factory_type", result)
        self.assertIn("model_id", result)
        self.assertIn("supported", result)
        self.assertIn("available", result)
        self.assertIn("errors", result)
        
        self.assertEqual(result["factory_type"], "ollama")
        self.assertEqual(result["model_id"], "nomic-embed-text")
        self.assertTrue(result["supported"])
    
    def test_validate_model_config_with_invalid_factory_type(self):
        """Testa validate_model_config com tipo de factory inválido."""
        result = self.factory.validate_model_config("invalid_type", "test_model")
        
        self.assertFalse(result["valid"])
        self.assertFalse(result["supported"])
        self.assertGreater(len(result["errors"]), 0)
        self.assertIn("não suportado", result["errors"][0])
    
    def test_validate_model_config_with_empty_model_id(self):
        """Testa validate_model_config com model_id vazio."""
        result = self.factory.validate_model_config("ollama", "")
        
        self.assertFalse(result["valid"])
        self.assertGreater(len(result["errors"]), 0)
        self.assertIn("ID do modelo não pode estar vazio", result["errors"][0])
    
    def test_validate_model_config_with_empty_factory_type(self):
        """Testa validate_model_config com factory_type vazio."""
        result = self.factory.validate_model_config("", "test_model")
        
        self.assertFalse(result["valid"])
        self.assertFalse(result["supported"])
        self.assertGreater(len(result["errors"]), 0)
    
    @patch('agno.embedder.ollama.OllamaEmbedder')
    def test_create_ollama_model_success(self, mock_ollama):
        """Testa criação bem-sucedida de modelo Ollama."""
        # Setup mock
        mock_instance = MagicMock()
        mock_ollama.return_value = mock_instance
        
        # Executar
        result = self.factory.create_model("ollama", "nomic-embed-text")
        
        # Verificar
        self.assertEqual(result, mock_instance)
        mock_ollama.assert_called_once_with(id="nomic-embed-text")
    
    def test_create_model_with_empty_factory_type_raises_error(self):
        """Testa se create_model levanta erro com factory_type vazio."""
        with self.assertRaises(ValueError) as context:
            self.factory.create_model("", "test_model")
        
        self.assertIn("Tipo de modelo não pode estar vazio", str(context.exception))
    
    def test_create_model_with_empty_model_id_raises_error(self):
        """Testa se create_model levanta erro com model_id vazio."""
        with self.assertRaises(ValueError) as context:
            self.factory.create_model("ollama", "")
        
        self.assertIn("ID do modelo não pode estar vazio", str(context.exception))
    
    def test_create_model_with_unsupported_type_raises_error(self):
        """Testa se create_model levanta erro com tipo não suportado."""
        with self.assertRaises(ValueError) as context:
            self.factory.create_model("unsupported_type", "test_model")
        
        self.assertIn("não suportado", str(context.exception))
    
    def test_create_model_with_case_insensitive_factory_type(self):
        """Testa se create_model funciona com factory_type case insensitive."""
        with patch('agno.embedder.ollama.OllamaEmbedder') as mock_ollama:
            mock_instance = MagicMock()
            mock_ollama.return_value = mock_instance
            
            # Testar com diferentes casos
            result1 = self.factory.create_model("OLLAMA", "test")
            result2 = self.factory.create_model("Ollama", "test")
            result3 = self.factory.create_model("ollama", "test")
            
            # Todos devem retornar a mesma instância mockada
            self.assertEqual(result1, mock_instance)
            self.assertEqual(result2, mock_instance)
            self.assertEqual(result3, mock_instance)
    
    @patch.dict(os.environ, {'GEMINI_API_KEY': 'test_key'})
    @patch('agno.embedder.google.GeminiEmbedder')
    def test_create_gemini_model_with_api_key(self, mock_gemini):
        """Testa criação de modelo Gemini com API key configurada."""
        mock_instance = MagicMock()
        mock_gemini.return_value = mock_instance
        
        result = self.factory.create_model("gemini", "models/embedding-001")
        
        self.assertEqual(result, mock_instance)
        mock_gemini.assert_called_once_with(id="models/embedding-001", api_key="test_key")
    
    def test_gemini_model_without_api_key_raises_error(self):
        """Testa se criar modelo Gemini sem API key levanta erro."""
        # Garantir que a variável de ambiente não existe
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ValueError) as context:
                self.factory._get_model_class("gemini")
            
            self.assertIn("GEMINI_API_KEY não está configurado", str(context.exception))
    
    def test_create_model_with_additional_kwargs(self):
        """Testa create_model com parâmetros adicionais."""
        with patch('agno.embedder.ollama.OllamaEmbedder') as mock_ollama:
            mock_instance = MagicMock()
            mock_ollama.return_value = mock_instance
            
            # Executar com kwargs adicionais
            result = self.factory.create_model(
                "ollama", 
                "nomic-embed-text", 
                custom_param="test_value",
                another_param=123
            )
            
            # Verificar que os kwargs foram passados
            mock_ollama.assert_called_once_with(
                id="nomic-embed-text",
                custom_param="test_value",
                another_param=123
            )
            self.assertEqual(result, mock_instance)
    
    def test_is_available_model_with_ollama(self):
        """Testa is_available_model com Ollama (deve estar disponível)."""
        result = self.factory.is_available_model("ollama")
        self.assertTrue(result)
    
    def test_is_available_model_with_invalid_model(self):
        """Testa is_available_model com modelo inválido."""
        result = self.factory.is_available_model("invalid_model")
        self.assertFalse(result)


if __name__ == "__main__":
    unittest.main()
