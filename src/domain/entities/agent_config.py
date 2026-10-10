from collections.abc import Mapping
from dataclasses import dataclass
from typing import List, Optional

from src.domain.entities.model_config import ModelConfig, ParamValue
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
    # Opcionais do documento (F2-01); consumidores leem ``model_config``.
    model_params: Optional[Mapping[str, ParamValue]] = None
    base_url: Optional[str] = None
    api_key_ref: Optional[str] = None

    def __post_init__(self):
        require_text(self.id, "ID do agente")
        require_text(self.nome, "Nome do agente")
        require_text(self.model, "Modelo do agente")
        require_text(self.factory_ia_model, "Factory do modelo do agente")
        require_optional_text(self.descricao, "Descrição do agente")
        _ = self.model_config  # valida os campos do modelo

    @property
    def model_config(self) -> ModelConfig:
        """Modelo do agente em formato neutro (``factory_ia_model`` + ``model`` + opcionais)."""
        return ModelConfig(
            provider=self.factory_ia_model,
            model_id=self.model,
            params=self.model_params,
            base_url=self.base_url,
            api_key_ref=self.api_key_ref,
        )
