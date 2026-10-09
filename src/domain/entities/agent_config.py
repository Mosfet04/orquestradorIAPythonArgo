from dataclasses import dataclass
from typing import List, Optional

from src.domain.entities.rag_config import RagConfig
from src.domain.entities.validation import require_optional_text, require_text


@dataclass
class AgentConfig:
    """Entidade que representa a configuração de um agente."""

    id: str
    nome: str
    factory_ia_model: str
    model: str
    descricao: Optional[str]
    prompt: str
    tools_ids: Optional[List[str]] = None
    rag_config: Optional[RagConfig] = None
    user_memory_active: bool = False
    summary_active: bool = False
    active: bool = True

    def __post_init__(self):
        require_text(self.id, "ID do agente")
        require_text(self.nome, "Nome do agente")
        require_text(self.model, "Modelo do agente")
        require_text(self.factory_ia_model, "Factory do modelo do agente")
        require_optional_text(self.descricao, "Descrição do agente")
