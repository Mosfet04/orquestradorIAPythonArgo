"""Log de auditoria de tool call (F4-03): um evento estruturado por chamada, para as tools HTTP e MCP.

``audited_entrypoint`` envolve a chamada da fonte (que devolve ``ToolCallOutcome``) e é o entrypoint da
``Function`` do agno. Quem chamou vem do que o agno injeta pelos parâmetros ``_agno_run_context``,
``_agno_agent`` e ``_agno_team`` (agno 3.1.12, ``tools/function.py``, ``_build_entrypoint_args``; argumento do
modelo com esses nomes é descartado pelo agno em ``_drop_injected_overrides``). Fora de um run (chamada direta),
os campos de identidade ficam ``None``. O ``trace_id``/``span_id`` do OTel entram pelo processador do structlog.

O evento leva: ``audit="tool_call"`` (filtro), ``source`` (``http``/``mcp``), ``tool_id`` (documento),
``function`` (nome exposto ao modelo), ``mcp_tool`` (nome no servidor, só MCP), ``result``
(``ok``/``error``/``refused``/``cancelled``), ``reason`` (código fixo), ``error_type``, ``http_status``,
``duration_ms``, ``arguments`` (nomes presentes que o schema declara, em ordem) e ``undeclared_arguments``
(quantos outros vieram), mais ``agent_id``, ``team_id``, ``user_id``, ``run_id`` e ``session_id``.
Nunca valor de argumento, header, segredo, corpo/resultado nem texto de exceção: o nome não declarado é escolhido
pelo modelo (texto livre), por isso só a contagem; o hash dos valores ficou de fora porque valor de pouca entropia
(CPF, CEP) sai do hash por força bruta. ``ok`` vai em ``info``; o resto, em ``warning``.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable, Collection
from typing import Any, Literal

from src.domain.ports import ILogger
from src.infrastructure.tools.call_outcome import ToolCallOutcome

AUDIT_MESSAGE = "Auditoria de tool call"
ToolSourceKind = Literal["http", "mcp"]
ToolCall = Callable[[dict[str, Any]], Awaitable[ToolCallOutcome]]


def audited_entrypoint(
    call: ToolCall,
    *,
    logger: ILogger,
    source: ToolSourceKind,
    tool_id: str,
    function_name: str,
    declared_arguments: Collection[str],
    mcp_tool: str | None = None,
) -> Callable[..., Awaitable[str]]:
    """Entrypoint async que chama ``call`` com os argumentos do modelo, registra o evento e devolve o texto."""
    declared = frozenset(declared_arguments)
    fixed: dict[str, Any] = {
        "audit": "tool_call",
        "source": source,
        "tool_id": tool_id,
        "function": function_name,
        "mcp_tool": mcp_tool,
    }

    def emit(
        started: float,
        kwargs: dict[str, Any],
        injected: tuple[object, object, object],
        result: str,
        reason: str | None = None,
        error_type: str | None = None,
        http_status: int | None = None,
    ) -> None:
        run_context, agent, team = injected
        present = sorted(name for name in kwargs if name in declared)
        log = logger.info if result == "ok" else logger.warning
        log(
            AUDIT_MESSAGE,
            **fixed,
            result=result,
            reason=reason,
            error_type=error_type,
            http_status=http_status,
            duration_ms=round((time.perf_counter() - started) * 1000, 1),
            arguments=present,
            undeclared_arguments=len(kwargs) - len(present),
            agent_id=getattr(agent, "id", None),
            team_id=getattr(team, "id", None),
            user_id=getattr(run_context, "user_id", None),
            run_id=getattr(run_context, "run_id", None),
            session_id=getattr(run_context, "session_id", None),
        )

    async def entrypoint(
        _agno_run_context: object = None, _agno_agent: object = None, _agno_team: object = None, **kwargs: Any
    ) -> str:
        """Chama a tool e registra o evento de auditoria (inclusive se a chamada for cancelada ou levantar)."""
        started = time.perf_counter()
        injected = (_agno_run_context, _agno_agent, _agno_team)
        try:
            outcome = await call(kwargs)
        except asyncio.CancelledError:
            emit(started, kwargs, injected, "cancelled")
            raise
        except Exception as exc:
            emit(started, kwargs, injected, "error", "unexpected", type(exc).__name__)
            raise
        emit(started, kwargs, injected, outcome.result, outcome.reason, outcome.error_type, outcome.http_status)
        return outcome.text

    return entrypoint
