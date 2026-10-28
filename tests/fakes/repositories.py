"""Repositórios em memória para as portas de ``src/domain/repositories`` (e a árvore de documentos).

Mesma semântica dos adapters Mongo e YAML (suíte de ``tests/contract``): filtros por ``active``,
ordem de inserção (a ordem estável do backend), id repetido mantido nas listagens de agentes e teams
(o caso de uso fica com o primeiro), uma tool por id (a primeira), busca por id devolvendo o primeiro,
erro/``None`` para id inexistente e cópias independentes a cada leitura (como um documento novo
vindo do banco). Escrita com concorrência otimista (FA-01): item semeado é a versão 0 (legado) e
cada ``save`` grava a próxima; versão divergente levanta ``ConfigVersionConflict``.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable
from typing import Generic, Protocol, TypeVar

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.document_node import DocumentNode
from src.domain.entities.team_config import TeamConfig
from src.domain.entities.tool import Tool
from src.domain.ports.document_tree_repository_port import IDocumentTreeRepository
from src.domain.repositories.agent_config_repository import IAgentConfigRepository
from src.domain.repositories.team_config_repository import ITeamConfigRepository
from src.domain.repositories.tool_repository import IToolRepository
from src.domain.repositories.versioning import ConfigVersionConflict, Versioned


class _HasId(Protocol):
    id: str


E = TypeVar("E", bound=_HasId)


class _VersionedItems(Generic[E]):
    """Itens na ordem de inserção, cada um com a versão; a escrita age no item do id na versão esperada.

    Como o filtro ``{"id", "version"}`` do Mongo: com id repetido, cada versão aponta um documento.
    """

    def __init__(self, items: Iterable[E] = ()) -> None:
        self._entries: list[Versioned[E]] = [Versioned(copy.deepcopy(item), 0) for item in items]

    @property
    def _items(self) -> list[E]:
        return [entry.entity for entry in self._entries]

    def _position(self, entity_id: str, expected_version: int) -> int | None:
        """Posição do primeiro item do id na versão, ``None`` para id novo na versão 0; senão, conflito."""
        valid_version = isinstance(expected_version, int) and not isinstance(expected_version, bool)
        if not isinstance(entity_id, str) or not valid_version:
            raise TypeError("id da config deve ser texto e a versão, inteiro")
        same_id = [i for i, entry in enumerate(self._entries) if entry.entity.id == entity_id]
        position = next((i for i in same_id if self._entries[i].version == expected_version), None)
        if position is None and (same_id or expected_version != 0):
            raise ConfigVersionConflict(entity_id, expected_version)
        return position

    async def list_all(self) -> list[Versioned[E]]:
        return copy.deepcopy(self._entries)

    async def save(self, entity: E, expected_version: int) -> int:
        position = self._position(entity.id, expected_version)
        saved = Versioned(copy.deepcopy(entity), expected_version + 1)
        if position is None:
            self._entries.append(saved)
        else:
            self._entries[position] = saved
        return saved.version

    async def delete(self, entity_id: str, expected_version: int) -> None:
        position = self._position(entity_id, expected_version)
        if position is None:
            raise ConfigVersionConflict(entity_id, expected_version)
        del self._entries[position]


class InMemoryAgentConfigRepository(_VersionedItems[AgentConfig], IAgentConfigRepository):
    async def get_active_agents(self) -> list[AgentConfig]:
        return [copy.deepcopy(c) for c in self._items if c.active]

    async def get_agent_by_id(self, agent_id: str) -> AgentConfig:
        found = next((c for c in self._items if c.id == agent_id), None)
        if found is None:
            raise ValueError(f"Agente {agent_id} não encontrado")
        return copy.deepcopy(found)


class InMemoryTeamConfigRepository(_VersionedItems[TeamConfig], ITeamConfigRepository):
    async def get_active_teams(self) -> list[TeamConfig]:
        return [copy.deepcopy(c) for c in self._items if c.active]

    async def get_team_by_id(self, team_id: str) -> TeamConfig | None:
        found = next((c for c in self._items if c.id == team_id), None)
        return copy.deepcopy(found) if found else None


class InMemoryToolRepository(_VersionedItems[Tool], IToolRepository):
    def _first_active_per_id(self, wanted: set[str] | None) -> list[Tool]:
        unique: dict[str, Tool] = {}
        for tool in self._items:
            if tool.active and (wanted is None or tool.id in wanted):
                unique.setdefault(tool.id, tool)
        return [copy.deepcopy(t) for t in unique.values()]

    async def get_tools_by_ids(self, tool_ids: list[str]) -> list[Tool]:
        return self._first_active_per_id(set(tool_ids or ())) if tool_ids else []

    async def get_tool_by_id(self, tool_id: str) -> Tool:
        found = next((t for t in self._items if t.id == tool_id), None)
        if found is None:
            raise ValueError(f"Tool {tool_id} não encontrada")
        return copy.deepcopy(found)

    async def get_all_active_tools(self) -> list[Tool]:
        return self._first_active_per_id(None)


class InMemoryDocumentTreeRepository(IDocumentTreeRepository):
    """Ordem de gravação (lote a lote, e a da lista dentro do lote); id repetido: vence o primeiro."""

    def __init__(self) -> None:
        self._nodes: list[DocumentNode] = []

    async def save_nodes(self, nodes: list[DocumentNode]) -> None:
        stored = {n.id for n in self._nodes}
        for node in nodes:
            if node.id not in stored:
                stored.add(node.id)
                self._nodes.append(copy.deepcopy(node))

    async def get_root_nodes(self, doc_name: str) -> list[DocumentNode]:
        return [copy.deepcopy(n) for n in self._nodes if n.doc_name == doc_name and n.level == 0]

    async def get_children(self, parent_id: str) -> list[DocumentNode]:
        return [copy.deepcopy(n) for n in self._nodes if n.parent_id == parent_id]

    async def get_node(self, node_id: str) -> DocumentNode | None:
        found = next((n for n in self._nodes if n.id == node_id), None)
        return copy.deepcopy(found) if found else None

    async def exists(self, doc_name: str) -> bool:
        return any(n.doc_name == doc_name for n in self._nodes)

    async def has_empty_embedding(self, doc_name: str) -> bool:
        return any(n.doc_name == doc_name and n.embedding == [] for n in self._nodes)

    async def delete_document(self, doc_name: str) -> int:
        kept = [n for n in self._nodes if n.doc_name != doc_name]
        deleted = len(self._nodes) - len(kept)
        self._nodes[:] = kept
        return deleted
