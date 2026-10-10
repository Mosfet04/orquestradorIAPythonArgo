"""Adapter do runtime de agentes sobre o agno 2.5: o único lugar de ``src`` que monta Agent/Team.

- ``runtime``: ``AgnoRuntime``, implementação da porta ``AgentRuntime`` e ciclo de vida do AgentOS
  no app (``mount``/``start``/``close``);
- ``agent_factory_service``/``team_factory_service``: montagem de ``Agent``/``Team``;
- ``user_id_guardrail``: recusa run sem ``user_id`` de entidade com memória de usuário;
- ``hierarchical_search_tool``: ``Toolkit`` da busca hierárquica;
- ``http_tool_factory``: ``Function`` do agno para cada tool HTTP (porta ``IToolFactory``);
- ``llm_summary_generator``: sumário de seção pelo ``Model`` do agno (porta ``ISummaryGenerator``);
- ``agui_router``: rotas AG-UI por entidade, com o conversor de eventos do agno;
- ``run_cancellation``: gerenciador e rotas de cancelamento de run.

O import **estático** do agno só existe aqui: contrato ``agno-so-no-runtime`` do ``lint-imports``
(F2-07). Exceção conhecida, que o contrato não vê: ``providers/registry.py`` carrega por nome
(``importlib.import_module``) as classes ``agno.*`` das specs de ``providers/builtins.py``.
"""

from src.infrastructure.runtime.agno.runtime import AgnoRuntime

__all__ = ["AgnoRuntime"]
