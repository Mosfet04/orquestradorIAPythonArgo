"""Diário das escritas de config (FA-05): a auditoria de cada escrita e a revisão do store que as réplicas seguem.

Cada escrita pela API registra uma entrada (quem, quando, o quê e o que mudou) e incrementa a revisão do
store; cada réplica consulta a revisão e, se ela mudou, relê a config e troca o runtime. A entrada guarda o
documento como a API o mostra (valor de header mascarado): nunca o valor de um segredo nem a chave de quem
escreveu, só o id dela.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime


@dataclass(frozen=True)
class FieldChange:
    """Um campo de topo do documento antes e depois da escrita (``None`` quando ele não existia)."""

    before: object
    after: object


@dataclass(frozen=True)
class ConfigAuditEntry:
    """Uma escrita de config: tipo, id, versão do documento, id da chave (``actor``), data e o que mudou.

    ``version`` é a versão gravada (na remoção, a removida); ``document`` é o documento depois da escrita,
    como a API o mostra (``None`` na remoção), e ``diff`` traz só os campos que mudaram.
    """

    kind: str
    entity_id: str
    version: int
    actor: str
    at: datetime
    diff: Mapping[str, FieldChange]
    document: Mapping[str, object] | None


class ConfigAuditLog(ABC):
    """Onde ficam as entradas de auditoria e a revisão do store de config."""

    @abstractmethod
    async def record(self, entry: ConfigAuditEntry) -> int:
        """Incrementa a revisão do store e grava a entrada; devolve a revisão nova."""
        ...

    @abstractmethod
    async def history(self, kind: str, entity_id: str) -> list[ConfigAuditEntry]:
        """Entradas do id, da mais nova à mais antiga."""
        ...

    @abstractmethod
    async def revision(self) -> int:
        """Revisão atual do store (0 = nenhuma escrita registrada)."""
        ...
