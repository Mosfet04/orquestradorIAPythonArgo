"""Schema da config para a UI (FA-03): o que ``GET /admin/schema`` descreve, a partir do código em vigor.

Os providers chegam prontos do composition root (o registry em vigor: built-ins e plugins da allowlist);
modos de team, métodos HTTP, tipos de parâmetro, ``search_strategy`` e formatos de ``api_key_ref`` saem
das entidades do domínio, sem lista paralela. Nenhum valor de ambiente ou segredo entra no documento:
só nomes de opção e os formatos de referência.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

from src.domain.entities.model_config import SECRET_REF_FORMATS
from src.domain.entities.rag_config import SearchStrategy
from src.domain.entities.team_config import TEAM_MODES
from src.domain.entities.tool import HttpMethod, ParameterType


@dataclass(frozen=True)
class ProviderOption:
    """Id ou alias de provider que cria um tipo de modelo e o que a config pode pedir dele."""

    name: str
    model_params: tuple[str, ...]
    requires_base_url: bool


@dataclass(frozen=True)
class ConfigSchema:
    """Opções de provider por tipo (``chat``, ``embedder``) e se o store de config aceita escrita."""

    providers: Mapping[str, tuple[ProviderOption, ...]]
    config_store_writable: bool

    def document(self) -> dict[str, object]:
        """Documento JSON do schema, uma seção por campo do documento de config que ela alimenta.

        ``providers.chat``: ``factory_ia_model`` de agente e team (com ``model_params`` e ``base_url``);
        ``providers.embedder``: ``rag_config.factory_ia_model``; ``team.mode``; ``tool.http_method``;
        ``tool.parameter_type``: ``parameters[].type`` da tool; ``rag_config.search_strategy``;
        ``api_key_ref.formats``: formatos aceitos em ``api_key_ref`` (não é campo); ``config_store.writable``:
        se o store aceita escrita (não é campo; ``false`` = as rotas ``/admin/config`` respondem 409).
        """
        return {
            "providers": {
                kind: [
                    {
                        "name": option.name,
                        "model_params": list(option.model_params),
                        "requires_base_url": option.requires_base_url,
                    }
                    for option in options
                ]
                for kind, options in self.providers.items()
            },
            "team": {"mode": list(TEAM_MODES)},
            "tool": {
                "http_method": [method.value for method in HttpMethod],
                "parameter_type": [parameter_type.value for parameter_type in ParameterType],
            },
            "rag_config": {"search_strategy": [strategy.value for strategy in SearchStrategy]},
            "api_key_ref": {"formats": list(SECRET_REF_FORMATS)},
            "config_store": {"writable": self.config_store_writable},
        }
