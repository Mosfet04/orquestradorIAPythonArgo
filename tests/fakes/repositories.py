"""Repositórios em memória para as portas de ``src/domain/repositories`` (e a árvore de documentos).

Semântica espelhada dos adapters Mongo de ``src/infrastructure/repositories``:
filtros por ``active``, erro/``None`` para id inexistente e cópias independentes a
cada leitura (como um documento novo vindo do banco).
"""

from __future__ import annotations

import copy
from collections.abc import Iterable

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.document_node import DocumentNode
from src.domain.entities.team_config import TeamConfig
from src.domain.entities.tool import Tool
from src.domain.ports.document_tree_repository_port import IDocumentTreeRepository
from src.domain.repositories.agent_config_repository import IAgentConfigRepository
from src.domain.repositories.team_config_repository import ITeamConfigRepository
from src.domain.repositories.tool_repository import IToolRepository


class InMemoryAgentConfigRepository(IAgentConfigRepository):
    def __init__(self, configs: Iterable[AgentConfig] = ()) -> None:
        self._items: dict[str, AgentConfig] = {c.id: copy.deepcopy(c) for c in configs}

    async def get_active_agents(self) -> list[AgentConfig]:
        return [copy.deepcopy(c) for c in self._items.values() if c.active]

    async def get_agent_by_id(self, agent_id: str) -> AgentConfig:
        if agent_id not in self._items:
            raise ValueError(f"Agente {agent_id} não encontrado")
        return copy.deepcopy(self._items[agent_id])


class InMemoryTeamConfigRepository(ITeamConfigRepository):
    def __init__(self, configs: Iterable[TeamConfig] = ()) -> None:
        self._items: dict[str, TeamConfig] = {c.id: copy.deepcopy(c) for c in configs}

    async def get_active_teams(self) -> list[TeamConfig]:
        return [copy.deepcopy(c) for c in self._items.values() if c.active]

    async def get_team_by_id(self, team_id: str) -> TeamConfig | None:
        item = self._items.get(team_id)
        return copy.deepcopy(item) if item else None


class InMemoryToolRepository(IToolRepository):
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._items: dict[str, Tool] = {t.id: copy.deepcopy(t) for t in tools}

    async def get_tools_by_ids(self, tool_ids: list[str]) -> list[Tool]:
        wanted = set(tool_ids or ())
        return [copy.deepcopy(t) for t in self._items.values() if t.id in wanted and t.active]

    async def get_tool_by_id(self, tool_id: str) -> Tool:
        if tool_id not in self._items:
            raise ValueError(f"Tool {tool_id} não encontrada")
        return copy.deepcopy(self._items[tool_id])

    async def get_all_active_tools(self) -> list[Tool]:
        return [copy.deepcopy(t) for t in self._items.values() if t.active]


class InMemoryDocumentTreeRepository(IDocumentTreeRepository):
    def __init__(self) -> None:
        self._nodes: list[DocumentNode] = []

    async def save_nodes(self, nodes: list[DocumentNode]) -> None:
        self._nodes.extend(copy.deepcopy(n) for n in nodes)

    async def get_root_nodes(self, doc_name: str) -> list[DocumentNode]:
        return [copy.deepcopy(n) for n in self._nodes if n.doc_name == doc_name and n.level == 0]

    async def get_children(self, parent_id: str) -> list[DocumentNode]:
        return [copy.deepcopy(n) for n in self._nodes if n.parent_id == parent_id]

    async def get_node(self, node_id: str) -> DocumentNode | None:
        found = next((n for n in self._nodes if n.id == node_id), None)
        return copy.deepcopy(found) if found else None

    async def exists(self, doc_name: str) -> bool:
        return any(n.doc_name == doc_name for n in self._nodes)
