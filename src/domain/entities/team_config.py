"""Entidade de configuração de Team multi-agente."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import List, Optional

from src.domain.entities.validation import require_optional_text, require_text


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
        require_text(self.id, "ID do team")
        require_text(self.nome, "Nome do team")
        require_text(self.model, "Modelo do team")
        require_text(self.factory_ia_model, "Factory do modelo do team")
        require_optional_text(self.descricao, "Descrição do team")
        if not self.member_ids:
            raise ValueError("Team precisa de pelo menos um membro")
        if self.mode not in ("route", "coordinate", "broadcast", "tasks"):
            raise ValueError(
                f"Modo inválido: {self.mode!r}. "
                "Valores aceitos: route, coordinate, broadcast, tasks"
            )
