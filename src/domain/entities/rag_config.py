from dataclasses import dataclass
from typing import Optional


@dataclass
class RagConfig:
    """Entidade que representa a configuração de RAG (Retrieval-Augmented Generation)."""

    active: bool = False
    doc_name: Optional[str] = None
    model: Optional[str] = None
    factory_ia_model: Optional[str] = None