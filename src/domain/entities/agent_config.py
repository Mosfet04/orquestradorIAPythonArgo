from dataclasses import dataclass
from typing import Optional
from src.domain.entities.rag_config import RagConfig


@dataclass
class AgentConfig:
    """Entidade que representa a configuração de um agente."""
    
    id: str
    nome: str
    factoryIaModel: str
    model: str
    descricao: str
    prompt: str
    tools_ids: Optional[list[str]] = None
    rag_config: Optional[RagConfig]= None
    active: bool = True
    
    def __post_init__(self):
        if not self.id:
            raise ValueError("ID do agente não pode estar vazio")
        if not self.nome:
            raise ValueError("Nome do agente não pode estar vazio")
        if not self.model:
            raise ValueError("Modelo do agente não pode estar vazio")
        if not self.factoryIaModel:
            raise ValueError("Factory do modelo do agente não pode estar vazio")
