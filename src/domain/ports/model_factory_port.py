from abc import ABC, abstractmethod
from typing import Any, Dict


class InvalidModelConfigError(ValueError):
    """Config de modelo recusada por ``IModelFactory.validate_model_config``.

    A mensagem é só texto da validação (tipo/id do modelo da config, SDK ausente), nunca texto
    de exceção de SDK: pode ir ao log como motivo, como a de ``DocumentPathError``.
    """


class IModelFactory(ABC):
    """Interface para criação de modelos LLM."""

    @abstractmethod
    def create_model(self, factory_ia_model: str, model_id: str, **kwargs: Any) -> Any:
        ...

    @abstractmethod
    def validate_model_config(self, factory_ia_model: str, model_id: str) -> Dict[str, Any]:
        ...
