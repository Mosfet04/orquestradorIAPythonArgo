"""Adapter do runtime de agentes sobre o agno 2.5: o único lugar de ``src`` que monta Agent/Team.

- ``runtime``: ``AgnoRuntime``, implementação da porta ``AgentRuntime`` e ciclo de vida do AgentOS
  no app (``mount``/``start``/``close``);
- ``agent_factory_service``/``team_factory_service``: montagem de ``Agent``/``Team``;
- ``user_id_guardrail``: recusa run sem ``user_id`` de entidade com memória de usuário;
- ``hierarchical_search_tool``: ``Toolkit`` da busca hierárquica.
"""

from src.infrastructure.runtime.agno.runtime import AgnoRuntime

__all__ = ["AgnoRuntime"]
