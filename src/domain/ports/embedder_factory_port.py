from abc import ABC, abstractmethod
from typing import Any


class IEmbedderFactory(ABC):
    """Interface para criação de embedders de modelos."""

    @abstractmethod
    def create_model(self, factory_ia_model: str, model_id: str, **kwargs: Any) -> Any:
        ...
