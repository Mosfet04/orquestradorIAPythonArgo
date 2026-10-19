"""Escrita nas portas de config com concorrência otimista (FA-01).

Cada documento de config tem uma ``version`` inteira; documento sem ela (legado) é a versão 0.
Quem grava informa a versão que leu (``expected_version``): se o documento mudou nesse meio-tempo,
a escrita é recusada com ``ConfigVersionConflict`` e nada é gravado. Criar um id novo é gravar
com ``expected_version`` 0.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
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


class VersionedConfigRepository(ABC, Generic[T]):
    """Listagem e escrita com versão, iguais nas três portas de config (agentes, teams e tools)."""

    @abstractmethod
    async def list_all(self) -> list[Versioned[T]]:
        """Todos os documentos, inclusive inativos e ids repetidos, na ordem estável, com a versão."""
        ...

    @abstractmethod
    async def save(self, entity: T, expected_version: int) -> int:
        """Grava (cria ou substitui) o documento do id e devolve a nova versão.

        ``ConfigVersionConflict`` se nenhum documento do id estiver em ``expected_version`` (0 = id
        novo ou documento legado); ``ConfigStoreReadOnly`` se o store não aceitar escrita.
        """
        ...

    @abstractmethod
    async def delete(self, entity_id: str, expected_version: int) -> None:
        """Remove o documento do id na versão esperada; mesmos erros do ``save``."""
        ...
