from typing import Union, Dict, Any, Type
from agno.models.ollama import Ollama
import os


class ModelFactory:
    """Factory responsável por criar instâncias de modelos de IA baseado no tipo especificado."""
    
    @classmethod
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
        try:
            if factory_type == "ollama":
                return Ollama
            elif factory_type in ["openai"]:
                try:
                    from agno.models.openai.chat import OpenAIChat
                    return OpenAIChat
                except ImportError:
                    raise ValueError(
                        f"Modelo OpenAI não está disponível. "
                        f"Instale as dependências com: pip install openai"
                    )
            elif factory_type in ["anthropic"]:
                try:
                    from agno.models.anthropic.claude import Claude
                    return Claude
                except ImportError:
                    raise ValueError(
                        f"Modelo Anthropic não está disponível. "
                        f"Instale as dependências com: pip install anthropic"
                    )
            elif factory_type in ["gemini", "google"]:
                try:
                    # Verificar se as variáveis de ambiente estão configuradas
                    api_key_env = os.getenv("GEMINI_API_KEY")
                    if not api_key_env:
                        raise ValueError("GEMINI_API_KEY não está configurado no ambiente")
                    
                    from agno.models.google.gemini import Gemini
                    return Gemini
                except ImportError:
                    raise ValueError(
                        f"Modelo Gemini não está disponível. "
                        f"Instale as dependências com: pip install google-genai"
                    )
            elif factory_type in ["groq"]:
                try:
                    from agno.models.groq.chat import GroqChat
                    return GroqChat
                except ImportError:
                    raise ValueError(
                        f"Modelo Groq não está disponível. "
                        f"Instale as dependências com: pip install groq"
                    )
            elif factory_type in ["azure", "azureopenai"]:
                try:
                    from agno.models.azure.openai_chat import AzureOpenAIChat
                    return AzureOpenAIChat
                except ImportError:
                    raise ValueError(
                        f"Modelo Azure OpenAI não está disponível. "
                        f"Instale as dependências com: pip install openai"
                    )
            else:
                supported_models = ", ".join(cls.get_supported_models())
                raise ValueError(
                    f"Tipo de modelo '{factory_type}' não suportado. "
                    f"Modelos suportados: {supported_models}"
                )
                
        except ImportError as e:
            raise ValueError(
                f"Não foi possível importar o modelo '{factory_type}'. "
                f"Erro: {str(e)}"
            )
    
    @classmethod
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
        # Normalizar o nome do modelo para lowercase
        factory_type = factory_ia_model.lower().strip()
        
        # Validar entrada
        if not factory_type:
            raise ValueError("Tipo de modelo não pode estar vazio")
        
        if not model_id or not model_id.strip():
            raise ValueError("ID do modelo não pode estar vazio")
        
        # Obter a classe do modelo
        model_class = cls._get_model_class(factory_type)
        
        try:
            # Preparar parâmetros específicos para cada tipo de modelo
            if factory_type in ["gemini", "google"]:
                # Para Gemini, precisamos da API key
                api_key = kwargs.get('api_key') or os.getenv("GEMINI_API_KEY")
                if not api_key:
                    raise ValueError("GEMINI_API_KEY não está configurado no ambiente")
                
                # Remover api_key dos kwargs se estiver presente
                filtered_kwargs = {k: v for k, v in kwargs.items() if k != 'api_key'}
                return model_class(id=model_id, api_key=api_key, **filtered_kwargs)
            
            else:
                # Para outros modelos, usar configuração padrão
                return model_class(id=model_id, **kwargs)
                
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
