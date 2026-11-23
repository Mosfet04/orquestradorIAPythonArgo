"""Raciocínio fora do AG-UI por agente (``AgentConfig.expose_reasoning=false``, F3-10).

A fábrica dá ao agente com o campo desligado a ``PrivateReasoningTools`` no lugar da ``ReasoningTools``:
a origem marca as tools de raciocínio, não o nome. O run, a sessão e as rotas REST do AgentOS não mudam
(filtrar o raciocínio nelas é a F5); só o router AG-UI deixa de emitir os ``TOOL_CALL_*`` dessas tools:

- ``private_reasoning_tools`` diz, por agente servido (o próprio ou os membros de um team, em qualquer
  nível), que nomes de tool resolvem para a ``PrivateReasoningTools``. O agno registra o primeiro nome e
  pula o repetido (``agent/_tools.py``, ``parse_tools``, agno 3.1.12) e a fábrica põe a toolkit depois das
  tools do documento: uma tool do usuário chamada ``think`` é a que roda, e segue visível;
- ``track_private_reasoning_calls`` repassa o stream do agno sem mudança e guarda o ``tool_call_id`` das
  chamadas dessas tools (o evento leva o ``agent_id`` de quem chamou: em team, o do membro), antes de o
  conversor do AG-UI ver o evento; o filtro de saída do router descarta os ``TOOL_CALL_*`` com esses ids.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator, Mapping, MutableSet
from typing import TypeVar

from agno.agent import Agent
from agno.models.response import ToolExecution
from agno.run.agent import BaseAgentRunEvent
from agno.tools import Toolkit
from agno.tools.function import Function
from agno.tools.reasoning import ReasoningTools

from src.infrastructure.runtime.agno.served_entities import Entity

ChunkT = TypeVar("ChunkT")


class PrivateReasoningTools(ReasoningTools):
    """``ReasoningTools`` de agente com ``expose_reasoning=false``: as chamadas dela não vão ao cliente AG-UI."""


def private_reasoning_tools(entity: Entity) -> dict[str, frozenset[str]]:
    """``agent_id`` -> nomes de tool que, naquele agente, são da ``PrivateReasoningTools``.

    Agente sem ela fica fora. ``tools``/``members`` dinâmicos (callable do agno) a fábrica não monta: ficam
    fora também.
    """
    private: dict[str, frozenset[str]] = {}
    for agent in _agents(entity):
        names = _private_names(agent)
        if names and agent.id:
            private[agent.id] = names
    return private


def _agents(entity: Entity) -> Iterator[Agent]:
    if isinstance(entity, Agent):
        yield entity
        return
    if isinstance(entity.members, list):
        for member in entity.members:
            yield from _agents(member)


def _private_names(agent: Agent) -> frozenset[str]:
    """Nomes que resolvem para a ``PrivateReasoningTools``: o primeiro dono de cada nome, como no agno."""
    if not isinstance(agent.tools, list):
        return frozenset()
    seen: set[str] = set()
    private: set[str] = set()
    for tool in agent.tools:
        for name in _names(tool):
            if name in seen:
                continue
            seen.add(name)
            if isinstance(tool, PrivateReasoningTools):
                private.add(name)
    return frozenset(private)


def _names(tool: object) -> list[str]:
    """Nomes de função de uma tool do agente, na ordem do ``parse_tools`` (run assíncrono)."""
    if isinstance(tool, dict):
        return []  # tool nativa do provider: não roda no agente
    if isinstance(tool, Toolkit):
        return list(tool.get_async_functions())
    if isinstance(tool, Function):
        return [tool.name]
    name = getattr(tool, "__name__", None)
    return [name] if isinstance(name, str) else []


async def track_private_reasoning_calls(
    stream: AsyncIterator[ChunkT], private: Mapping[str, frozenset[str]], hidden_ids: MutableSet[str]
) -> AsyncIterator[ChunkT]:
    """Repassa ``stream`` e põe em ``hidden_ids`` o ``tool_call_id`` das chamadas de raciocínio privado."""
    async for chunk in stream:
        if private and isinstance(chunk, BaseAgentRunEvent):
            tool = getattr(chunk, "tool", None)
            if (
                isinstance(tool, ToolExecution)
                and tool.tool_call_id
                and tool.tool_name in private.get(chunk.agent_id, frozenset())
            ):
                hidden_ids.add(tool.tool_call_id)
        yield chunk
