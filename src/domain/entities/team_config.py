"""Entidade de configuração de Team multi-agente."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import List, Optional

from src.domain.entities.model_config import ModelConfig, ParamValue
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
    # Opcionais do documento (F2-01); consumidores leem ``model_config``.
    model_params: Optional[Mapping[str, ParamValue]] = None
    base_url: Optional[str] = None
    api_key_ref: Optional[str] = None

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
        _ = self.model_config  # valida os campos do modelo

    @property
    def model_config(self) -> ModelConfig:
        """Modelo líder em formato neutro (``factory_ia_model`` + ``model`` + opcionais)."""
        return ModelConfig(
            provider=self.factory_ia_model,
            model_id=self.model,
            params=self.model_params,
            base_url=self.base_url,
            api_key_ref=self.api_key_ref,
        )
