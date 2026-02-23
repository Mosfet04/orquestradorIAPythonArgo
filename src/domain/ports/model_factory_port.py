from abc import ABC, abstractmethod
from typing import Any, Dict


class IModelFactory(ABC):
    """Interface para criação de modelos LLM."""

    @abstractmethod
    def create_model(self, factory_ia_model: str, model_id: str, **kwargs: Any) -> Any:
        ...

    @abstractmethod
    def validate_model_config(self, factory_ia_model: str, model_id: str) -> Dict[str, Any]:
        ...
