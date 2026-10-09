"""Cancelamento de run só para run registrado (em andamento) — F1-10.

No agno 2.5.8 o gerenciador global em memória (``InMemoryRunCancellationManager``,
``agno/run/cancellation_management/in_memory_cancellation_manager.py``) grava ``run_id ->
True`` em todo ``cancel_run``, mesmo de run que não existe ("cancel-before-start"), e só
apaga a entrada quando um run com aquele id termina. As rotas ``POST
/agents|teams/{id}/runs/{run_id}/cancel`` do AgentOS sempre respondiam 200. Resultado: cada
cancel de id inventado ficava na memória para sempre e um cliente pré-cancelava o próximo run
com um ``runId`` que ele adivinha (no AG-UI o cliente escolhe o ``runId``). Aqui:

- ``RegisteredRunCancellationManager``: cancela só run registrado; para o resto devolve
  ``False`` sem guardar nada. O registro segue ``setdefault`` como no agno: o run de
  background, registrado antes de a task começar, continua cancelável;
- ``build_run_cancel_router``: as duas rotas de cancel. Entram no app antes do
  ``AgentOS.get_app()``, que pula as dele (``on_route_conflict="preserve_base_app"``,
  ``agno/os/app.py``, ``_add_router``). Entidade fora das montadas = 404; run não
  registrado (inexistente ou já encerrado) = 404; cancelado = 200 ``{}`` (como no agno).
  Entram depois de o ``AgentOS`` ser criado (o construtor valida ids): falha ali não deixa
  rota parcial no app.

Fica de fora: quem cancela não precisa ser o dono do run nem a entidade do path a dona do
run (o gerenciador do agno é global por ``run_id``); amarrar à credencial é do F5.
"""

from __future__ import annotations

import threading
from collections.abc import Sequence
from typing import Any

from agno.agent import Agent
from agno.exceptions import RunCancelledException
from agno.run.cancel import acancel_run, get_cancellation_manager, set_cancellation_manager
from agno.run.cancellation_management.base import BaseRunCancellationManager
from agno.team import Team
from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse

from src.domain.ports import ILogger

_RUN_NOT_FOUND = "run não encontrado ou já encerrado"
_ENTITY_NOT_FOUND = "entidade não encontrada"  # o mesmo do AG-UI (agui_router)
_MAX_LOGGED_RUN_ID = 64
_NOT_FOUND_RESPONSE: dict[int | str, dict[str, Any]] = {
    404: {"description": "Entidade inexistente ou run não registrado (inexistente ou encerrado)"}
}


class RegisteredRunCancellationManager(BaseRunCancellationManager):
    """Gerenciador em memória que só marca cancelamento de run registrado.

    Um lock só para as versões síncrona e assíncrona (o do agno usa um de cada, que não se
    excluem). O lock só protege operações de dicionário, sem I/O: não bloqueia o event loop.
    """

    def __init__(self) -> None:
        self._runs: dict[str, bool] = {}
        self._lock = threading.Lock()

    def register_run(self, run_id: str) -> None:
        """Registra o run; não desfaz um cancelamento já pedido para ele."""
        with self._lock:
            self._runs.setdefault(run_id, False)

    async def aregister_run(self, run_id: str) -> None:
        self.register_run(run_id)

    def cancel_run(self, run_id: str) -> bool:
        """Marca o run como cancelado; ``False`` (sem guardar nada) se ele não está registrado."""
        with self._lock:
            if run_id not in self._runs:
                return False
            self._runs[run_id] = True
            return True

    async def acancel_run(self, run_id: str) -> bool:
        return self.cancel_run(run_id)

    def is_cancelled(self, run_id: str) -> bool:
        with self._lock:
            return self._runs.get(run_id, False)

    async def ais_cancelled(self, run_id: str) -> bool:
        return self.is_cancelled(run_id)

    def cleanup_run(self, run_id: str) -> None:
        with self._lock:
            self._runs.pop(run_id, None)

    async def acleanup_run(self, run_id: str) -> None:
        self.cleanup_run(run_id)

    def raise_if_cancelled(self, run_id: str) -> None:
        if self.is_cancelled(run_id):
            # Mesma mensagem do gerenciador do agno: vira o conteúdo do RunCancelledEvent.
            raise RunCancelledException(f"Run {run_id} was cancelled")

    async def araise_if_cancelled(self, run_id: str) -> None:
        self.raise_if_cancelled(run_id)

    def get_active_runs(self) -> dict[str, bool]:
        with self._lock:
            return dict(self._runs)

    async def aget_active_runs(self) -> dict[str, bool]:
        return self.get_active_runs()


def install_run_cancellation_manager() -> RegisteredRunCancellationManager:
    """Põe o ``RegisteredRunCancellationManager`` como gerenciador global do agno (idempotente).

    Chame no startup, antes de qualquer run: runs registrados no gerenciador anterior não
    passam para o novo.
    """
    current = get_cancellation_manager()
    if isinstance(current, RegisteredRunCancellationManager):
        return current
    manager = RegisteredRunCancellationManager()
    set_cancellation_manager(manager)
    return manager


def build_run_cancel_router(agents: Sequence[Agent], teams: Sequence[Team], logger: ILogger) -> APIRouter:
    """``POST /agents/{agent_id}/runs/{run_id}/cancel`` e o de teams, no lugar dos do AgentOS.

    O id da entidade é conferido no request: o AgentOS pode definir ids na montagem, depois
    desta função. Mesmos path, ``operation_id`` e 200 ``{}`` das rotas do agno.
    """
    router = APIRouter()

    @router.post(
        "/agents/{agent_id}/runs/{run_id}/cancel",
        tags=["Agents"],
        operation_id="cancel_agent_run",
        summary="Cancel Agent Run",
        responses=_NOT_FOUND_RESPONSE,
    )
    async def cancel_agent_run(agent_id: str, run_id: str) -> JSONResponse:
        if not any(agent.id == agent_id for agent in agents):
            raise HTTPException(status_code=404, detail=_ENTITY_NOT_FOUND)
        return await _cancel(run_id, "agent_id", agent_id, logger)

    @router.post(
        "/teams/{team_id}/runs/{run_id}/cancel",
        tags=["Teams"],
        operation_id="cancel_team_run",
        summary="Cancel Team Run",
        responses=_NOT_FOUND_RESPONSE,
    )
    async def cancel_team_run(team_id: str, run_id: str) -> JSONResponse:
        if not any(team.id == team_id for team in teams):
            raise HTTPException(status_code=404, detail=_ENTITY_NOT_FOUND)
        return await _cancel(run_id, "team_id", team_id, logger)

    return router


async def _cancel(run_id: str, entity_key: str, entity_id: str, logger: ILogger) -> JSONResponse:
    if not await acancel_run(run_id):
        raise HTTPException(status_code=404, detail=_RUN_NOT_FOUND)
    # run_id registrado = o de um run em andamento (UUID do agno ou runId do AG-UI, já
    # restrito a [A-Za-z0-9_-]{1,64}); truncado mesmo assim, pois vem do path.
    logger.info(
        "Cancelamento de run pedido", run_id=run_id[:_MAX_LOGGED_RUN_ID], **{entity_key: entity_id}
    )
    return JSONResponse(content={}, status_code=200)
