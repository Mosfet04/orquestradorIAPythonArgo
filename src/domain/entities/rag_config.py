from dataclasses import dataclass
from pyexpat import model
from typing import Optional


@dataclass
class RagConfig:
    """Entidade que representa a configuração de RAG (Retrieval-Augmented Generation)."""

    active: bool = False
    doc_name: Optional[str] = None
    model: Optional[str] = None
    factoryIaModel: Optional[str] = None