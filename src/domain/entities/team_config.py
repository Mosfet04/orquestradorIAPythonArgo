"""Entidade de configuração de Team multi-agente."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional


@dataclass
class TeamConfig:
    """Configuração de um Team — orquestrador que roteia/coordena agentes."""

    id: str
    nome: str
    factory_ia_model: str
    model: str
    member_ids: List[str] = field(default_factory=list)
    mode: str = "route"
    descricao: Optional[str] = None
    prompt: Optional[str] = None
    user_memory_active: bool = True
    summary_active: bool = False
    active: bool = True

    def __post_init__(self) -> None:
        if not self.id:
            raise ValueError("ID do team não pode estar vazio")
        if not self.nome:
            raise ValueError("Nome do team não pode estar vazio")
        if not self.model:
            raise ValueError("Modelo do team não pode estar vazio")
        if not self.factory_ia_model:
            raise ValueError("Factory do modelo do team não pode estar vazio")
        if not self.member_ids:
            raise ValueError("Team precisa de pelo menos um membro")
        if self.mode not in ("route", "coordinate", "broadcast", "tasks"):
            raise ValueError(
                f"Modo inválido: {self.mode!r}. "
                "Valores aceitos: route, coordinate, broadcast, tasks"
            )
