from dataclasses import dataclass
from enum import Enum
from typing import Optional


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