from dataclasses import dataclass
from enum import Enum
from typing import Optional

DEFAULT_EMBEDDING_PROVIDER = "ollama"
DEFAULT_EMBEDDING_MODEL = "mxbai-embed-large"

# Dimensões conhecidas de modelos Ollama para embedding.
# O OllamaEmbedder do agno tem default 4096 que não bate com a maioria dos modelos.
OLLAMA_EMBEDDING_DIMENSIONS: dict[str, int] = {
    "mxbai-embed-large": 1024,
    "nomic-embed-text": 768,
    "all-minilm": 384,
    "snowflake-arctic-embed": 1024,
}


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

    @property
    def resolved_provider(self) -> str:
        """Provider do embedder com fallback para Ollama."""
        return self.factory_ia_model or DEFAULT_EMBEDDING_PROVIDER

    @property
    def resolved_model(self) -> str:
        """Modelo de embedding com fallback para mxbai-embed-large."""
        return self.model or DEFAULT_EMBEDDING_MODEL
