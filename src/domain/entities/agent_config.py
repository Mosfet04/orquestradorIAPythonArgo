from dataclasses import dataclass
from typing import Optional


@dataclass
class AgentConfig:
    """Entidade que representa a configuração de um agente."""
    
    id: str
    nome: str
    model: str
    descricao: str
    prompt: str
    tools_ids: Optional[list[str]] = None
    active: bool = True
    
    def __post_init__(self):
        if not self.id:
            raise ValueError("ID do agente não pode estar vazio")
        if not self.nome:
            raise ValueError("Nome do agente não pode estar vazio")
        if not self.model:
            raise ValueError("Modelo do agente não pode estar vazio")
