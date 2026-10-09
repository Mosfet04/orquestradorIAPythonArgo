from collections.abc import Mapping
from dataclasses import dataclass
from enum import Enum
from typing import Optional

from src.domain.entities.model_config import ModelConfig, ParamValue

# Embedder usado quando o documento não define provider/modelo do RAG.
DEFAULT_EMBEDDER_PROVIDER = "ollama"
DEFAULT_EMBEDDER_MODEL = "nomic-embed-text:latest"


class SearchStrategy(Enum):
    """Estratégia de busca no knowledge base."""

    SEMANTIC = "semantic"
    HIERARCHICAL = "hierarchical"


@dataclass
class RagConfig:
    """Entidade que representa a configuração de RAG (Retrieval-Augmented Generation)."""

    active: bool = False
    doc_name: Optional[str] = None
    model: Optional[str] = None
    factory_ia_model: Optional[str] = None
    search_strategy: SearchStrategy = SearchStrategy.SEMANTIC
    # Opcionais do documento (F2-01); consumidores leem ``model_config``.
    model_params: Optional[Mapping[str, ParamValue]] = None
    base_url: Optional[str] = None
    api_key_ref: Optional[str] = None

    def __post_init__(self) -> None:
        has_extras = any(
            value is not None for value in (self.model_params, self.base_url, self.api_key_ref)
        )
        if self.model_config is None and has_extras:
            raise ValueError(
                "RAG com model_params, base_url ou api_key_ref exige model e factory_ia_model"
            )

    @property
    def model_config(self) -> Optional[ModelConfig]:
        """Embedder em formato neutro; ``None`` sem ``factory_ia_model`` e ``model`` (texto não vazio)."""
        provider, model_id = self.factory_ia_model, self.model
        if not (isinstance(provider, str) and provider and isinstance(model_id, str) and model_id):
            return None
        return ModelConfig(
            provider=provider,
            model_id=model_id,
            params=self.model_params,
            base_url=self.base_url,
            api_key_ref=self.api_key_ref,
        )

    def embedder_model_config(self) -> ModelConfig:
        """``model_config`` ou, sem ele, os defaults para o provider/modelo que faltar."""
        return self.model_config or ModelConfig(
            provider=self.factory_ia_model or DEFAULT_EMBEDDER_PROVIDER,
            model_id=self.model or DEFAULT_EMBEDDER_MODEL,
        )
