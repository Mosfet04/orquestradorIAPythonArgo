"""Repositórios em memória para as portas de ``src/domain/repositories`` (e a árvore de documentos).

Mesma semântica dos adapters Mongo e YAML (suíte de ``tests/contract``): filtros por ``active``,
ordem de inserção (a ordem estável do backend), id repetido mantido nas listagens de agentes e teams
(o caso de uso fica com o primeiro), uma tool por id (a primeira), busca por id devolvendo o primeiro,
erro/``None`` para id inexistente e cópias independentes a cada leitura (como um documento novo
vindo do banco).
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
        self._items: list[AgentConfig] = [copy.deepcopy(c) for c in configs]

    async def get_active_agents(self) -> list[AgentConfig]:
        return [copy.deepcopy(c) for c in self._items if c.active]

    async def get_agent_by_id(self, agent_id: str) -> AgentConfig:
        found = next((c for c in self._items if c.id == agent_id), None)
        if found is None:
            raise ValueError(f"Agente {agent_id} não encontrado")
        return copy.deepcopy(found)


class InMemoryTeamConfigRepository(ITeamConfigRepository):
    def __init__(self, configs: Iterable[TeamConfig] = ()) -> None:
        self._items: list[TeamConfig] = [copy.deepcopy(c) for c in configs]

    async def get_active_teams(self) -> list[TeamConfig]:
        return [copy.deepcopy(c) for c in self._items if c.active]

    async def get_team_by_id(self, team_id: str) -> TeamConfig | None:
        found = next((c for c in self._items if c.id == team_id), None)
        return copy.deepcopy(found) if found else None


class InMemoryToolRepository(IToolRepository):
    def __init__(self, tools: Iterable[Tool] = ()) -> None:
        self._items: list[Tool] = [copy.deepcopy(t) for t in tools]

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
