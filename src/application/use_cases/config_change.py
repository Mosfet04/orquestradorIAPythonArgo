"""Mudança de config aplicada à config ativa antes de montar o runtime (FA-04).

A escrita monta todas as entidades ativas com a mudança e só grava se a entidade alterada monta: os casos de
uso de carga recebem a ``ConfigChange`` e recusam com ``ConfigNotBuildable`` (depois do log da falha, como no
startup). As outras entidades seguem a política de sempre: a que falha fica de fora, com log.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Generic, TypeVar

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.team_config import TeamConfig

C = TypeVar("C", AgentConfig, TeamConfig)


class ConfigNotBuildable(Exception):
    """A config alterada não monta no runtime: nada foi gravado e o runtime anterior segue.

    ``error_type`` é o tipo do erro da montagem; o texto dele fica só no log (pode trazer segredo de SDK).
    """

    def __init__(self, error_type: str) -> None:
        super().__init__("Configuração não monta no runtime")
        self.error_type = error_type


@dataclass(frozen=True)
class ConfigChange(Generic[C]):
    """O documento ``entity_id`` passa a ser ``config``; sem ela (removido) ou inativa, deixa de ser servido."""

    entity_id: str
    config: C | None = None

    def applied(self, active: Sequence[C]) -> list[C]:
        """``active`` com a mudança: no lugar do id (no fim, se o id é novo) ou sem ele."""
        config = self.config
        if config is None or not config.active:
            return [item for item in active if item.id != self.entity_id]
        applied = [config if item.id == self.entity_id else item for item in active]
        if not any(item.id == self.entity_id for item in active):
            applied.append(config)
        return applied
