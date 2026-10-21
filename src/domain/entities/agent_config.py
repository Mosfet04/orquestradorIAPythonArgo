"""Entidade de configuração de um agente (documento ``agents_config``)."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import List, Optional

from src.domain.entities.model_config import ModelConfig, ParamValue
from src.domain.entities.rag_config import RagConfig
from src.domain.entities.validation import (
    require_bool,
    require_int_in_range,
    require_optional_text,
    require_text,
)

NUM_HISTORY_RUNS_RANGE = (1, 50)
"""Faixa de ``num_history_runs`` (validação e ``/admin/schema``). Sem 0: no agno é o histórico inteiro."""


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
    # Execução (FA-07): antes fixos na montagem; o default é o comportamento de antes.
    markdown: bool = True
    add_history_to_context: bool = True
    num_history_runs: int = 5
    add_datetime_to_context: bool = True
    # Raciocínio passo a passo do agno: mais chamadas ao modelo por resposta. No agno 2.5.8, até 10 passos
    # (``reasoning_max_steps``, sem teto configurável aqui) e as tools do agente podem ser chamadas nessa fase:
    # evite com tool não idempotente (POST/PUT/DELETE).
    reasoning: bool = False

    def __post_init__(self):
        require_text(self.id, "id", "ID do agente")
        require_text(self.nome, "nome", "Nome do agente")
        require_text(self.model, "model", "Modelo do agente")
        require_text(self.factory_ia_model, "factory_ia_model", "Factory do modelo do agente")
        require_optional_text(self.descricao, "descricao", "Descrição do agente")
        require_bool(self.markdown, "markdown")
        require_bool(self.add_history_to_context, "add_history_to_context")
        require_int_in_range(self.num_history_runs, "num_history_runs", *NUM_HISTORY_RUNS_RANGE)
        require_bool(self.add_datetime_to_context, "add_datetime_to_context")
        require_bool(self.reasoning, "reasoning")
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
