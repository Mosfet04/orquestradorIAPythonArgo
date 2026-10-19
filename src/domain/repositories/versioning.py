"""Escrita nas portas de config com concorrência otimista (FA-01).

Cada documento de config tem uma ``version`` inteira; documento sem ela (legado) é a versão 0.
Quem grava informa a versão que leu (``expected_version``): se o documento mudou nesse meio-tempo,
a escrita é recusada com ``ConfigVersionConflict`` e nada é gravado. Criar um id novo é gravar
com ``expected_version`` 0.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Generic, TypeVar

T = TypeVar("T")


@dataclass(frozen=True)
class Versioned(Generic[T]):
    """Entidade de config e a versão do documento de onde ela veio."""

    entity: T
    version: int


class ConfigVersionConflict(Exception):
    """O documento não está na versão esperada (mudou, foi removido ou já existe)."""

    def __init__(self, entity_id: str, expected_version: int) -> None:
        super().__init__(f"Config {entity_id!r} não está na versão {expected_version}")
        self.entity_id = entity_id
        self.expected_version = expected_version


class ConfigStoreReadOnly(Exception):
    """O store de config em uso não aceita escrita."""

    def __init__(self) -> None:
        super().__init__("O store de configuração é somente leitura")
