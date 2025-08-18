from typing import Dict, Any, Type
from agno.models.ollama import Ollama
import os
from src.infrastructure.logging import LoggerFactory, log_execution, log_ai_interaction


class ModelFactory:
    """Factory responsável por criar instâncias de modelos de IA baseado no tipo especificado."""
    
    logger = LoggerFactory.get_logger("model_factory")
    
    @classmethod
    @log_ai_interaction(logger_name="model_factory")
    def _get_model_class(cls, factory_type: str) -> Type:
        """
        Retorna a classe do modelo baseado no tipo, com import dinâmico.
        
        Args:
            factory_type: Tipo do modelo normalizado
            
        Returns:
            Classe do modelo
            
        Raises:
            ValueError: Se o modelo não for suportado ou não puder ser importado
        """
        # Normalização de tipo e aliases
        ft = (factory_type or "").lower().strip()
        aliases = {"google": "gemini", "azureopenai": "azure"}
        ft = aliases.get(ft, ft)

        # Casos simples sem import dinâmico
        if ft == "ollama":
            return Ollama

        # Especificações de import: ft -> (module_path, class_name, pip_pkg, human_name)
        import_specs = {
            "openai": ("agno.models.openai.chat", "OpenAIChat", "openai", "OpenAI"),
            "anthropic": ("agno.models.anthropic.claude", "Claude", "anthropic", "Anthropic"),
            "gemini": ("agno.models.google.gemini", "Gemini", "google-genai", "Gemini"),
            "groq": ("agno.models.groq.chat", "GroqChat", "groq", "Groq"),
            "azure": ("agno.models.azure.openai_chat", "AzureOpenAIChat", "openai", "Azure OpenAI"),
        }

        if ft not in import_specs:
            supported_models = ", ".join(cls.get_supported_models())
            raise ValueError(
                f"Tipo de modelo '{factory_type}' não suportado. "
                f"Modelos suportados: {supported_models}"
            )

        # Pré-validação específica
        if ft == "gemini":
            api_key_env = os.getenv("GEMINI_API_KEY")
            if not api_key_env:
                cls.logger.error(f"GEMINI_API_KEY não configurada para modelo: {factory_type}")
                raise ValueError("GEMINI_API_KEY não está configurado no ambiente")

        module_path, class_name, pip_pkg, human_name = import_specs[ft]
        try:
            module = __import__(module_path, fromlist=[class_name])
            return getattr(module, class_name)
        except ImportError:
            raise ValueError(
                f"Modelo {human_name} não está disponível. "
                f"Instale as dependências com: pip install {pip_pkg}"
            )
        except Exception as e:
            raise ValueError(
                f"Não foi possível importar o modelo '{factory_type}'. "
                f"Erro: {str(e)}"
            )
    
    @classmethod
    @log_ai_interaction(logger_name="model_factory")
    @log_execution(logger_name="model_factory", include_args=True)
    def create_model(cls, factory_ia_model: str, model_id: str, **kwargs) -> Any:
        """
        Cria uma instância do modelo baseado no tipo especificado.

        Args:
            factory_ia_model: Tipo do modelo (ex: "ollama", "openai", "gemini")
            model_id: ID/nome do modelo específico
            **kwargs: Parâmetros adicionais para configuração do modelo

        Returns:
            Instância do modelo configurado

        Raises:
            ValueError: Se o tipo de modelo não for suportado
        """
        factory_type = factory_ia_model.lower().strip()

        def validate_inputs():
            if not factory_type:
                cls.logger.error("Tipo de modelo vazio fornecido")
                raise ValueError("Tipo de modelo não pode estar vazio")
            if not model_id or not model_id.strip():
                cls.logger.error("ID do modelo vazio fornecido", factory_type=factory_type)
                raise ValueError("ID do modelo não pode estar vazio")

        def get_api_key():
            return kwargs.get('api_key') or os.getenv(f"{factory_type.upper()}_API_KEY")

        def instantiate_model(model_class, api_key):
            if factory_type == "ollama":
                return model_class(id=model_id, **kwargs)
            if not api_key:
                raise ValueError(f"{factory_type.upper()}_API_KEY não está configurado no ambiente")
            filtered_kwargs = {k: v for k, v in kwargs.items() if k != 'api_key'}
            return model_class(id=model_id, api_key=api_key, **filtered_kwargs)

        try:
            validate_inputs()
            model_class = cls._get_model_class(factory_type)
            api_key = get_api_key()
            return instantiate_model(model_class, api_key)
        except Exception as e:
            raise ValueError(
                f"Erro ao criar modelo {factory_ia_model} com ID '{model_id}': {str(e)}"
            )
    
    @classmethod
    def get_supported_models(cls) -> list[str]:
        """Retorna lista dos tipos de modelos suportados."""
        return [
            "ollama",
            "openai", 
            "anthropic",
            "gemini",
            "google",  # alias para gemini
            "groq",
            "azure",
            "azureopenai"  # alias para azure
        ]
    
    @classmethod
    def get_available_models(cls) -> Dict[str, bool]:
        """
        Retorna um dicionário com todos os modelos suportados e sua disponibilidade.
        
        Returns:
            Dict[str, bool]: Chave = nome do modelo, Valor = disponível (True/False)
        """
        models_availability = {}
        
        for model_type in cls.get_supported_models():
            try:
                cls._get_model_class(model_type)
                models_availability[model_type] = True
            except ValueError:
                models_availability[model_type] = False
                
        return models_availability
    
    @classmethod
    def is_supported_model(cls, factory_ia_model: str) -> bool:
        """Verifica se um tipo de modelo é suportado."""
        factory_type = factory_ia_model.lower().strip()
        return factory_type in cls.get_supported_models()
    
    @classmethod
    def is_available_model(cls, factory_ia_model: str) -> bool:
        """Verifica se um tipo de modelo está disponível (dependências instaladas)."""
        factory_type = factory_ia_model.lower().strip()
        try:
            cls._get_model_class(factory_type)
            return True
        except ValueError:
            return False
    
    @classmethod
    def validate_model_config(cls, factory_ia_model: str, model_id: str) -> Dict[str, Any]:
        """
        Valida a configuração do modelo sem criar a instância.
        
        Args:
            factory_ia_model: Tipo do modelo
            model_id: ID do modelo
            
        Returns:
            Dict com informações de validação
        """
        result = {
            "valid": True,
            "factory_type": factory_ia_model.lower().strip(),
            "model_id": model_id,
            "supported": False,
            "available": False,
            "errors": []
        }
        
        # Verificar se o tipo é suportado
        if not cls.is_supported_model(factory_ia_model):
            result["valid"] = False
            supported_models = ", ".join(cls.get_supported_models())
            result["errors"].append(
                f"Tipo de modelo '{factory_ia_model}' não suportado. "
                f"Modelos suportados: {supported_models}"
            )
        else:
            result["supported"] = True
        
        # Verificar se o model_id não está vazio
        if not model_id or not model_id.strip():
            result["valid"] = False
            result["errors"].append("ID do modelo não pode estar vazio")
        
        # Tentar validar se o modelo pode ser importado
        if result["supported"]:
            try:
                cls._get_model_class(result["factory_type"])
                result["available"] = True
            except ValueError as e:
                result["valid"] = False
                result["available"] = False
                result["errors"].append(str(e))
        
        return result
