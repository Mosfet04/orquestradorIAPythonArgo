"""Rotas ``/eval-runs*`` desligadas (404) até a F8 — F3-08.

No agno 3.1.12 os evals da rota ``POST /eval-runs`` do AgentOS (``agno/os/routers/evals/utils.py``)
nascem com ``telemetry=True`` e mandam o evento à API do agno ignorando ``AGNO_TELEMETRY``
(``agno/eval/utils.py``, ``log_eval_telemetry``); decisão do dono: sem evals pela API até a F8. As
rotas daqui têm os mesmos path e método das do agno e entram no app antes do ``AgentOS.get_app()``,
que pula as dele (``on_route_conflict="preserve_base_app"``, ``agno/os/app.py``, ``_add_router``),
como as de cancel (``run_cancellation``). Ficam fora do OpenAPI.
"""

from __future__ import annotations

from fastapi import APIRouter, HTTPException

_NOT_FOUND = "recurso não encontrado"

EVAL_RUNS_ROUTES: tuple[tuple[str, str], ...] = (
    ("GET", "/eval-runs"),
    ("GET", "/eval-runs/{eval_run_id}"),
    ("DELETE", "/eval-runs"),
    ("PATCH", "/eval-runs/{eval_run_id}"),
    ("POST", "/eval-runs"),
)
"""Método e path de cada rota de evals do AgentOS (``agno/os/routers/evals/evals.py``)."""


async def _eval_runs_disabled() -> None:
    raise HTTPException(status_code=404, detail=_NOT_FOUND)


def build_eval_runs_disabled_router() -> APIRouter:
    """Um 404 para cada rota de ``EVAL_RUNS_ROUTES``, sem ler corpo nem parâmetro."""
    router = APIRouter()
    for method, path in EVAL_RUNS_ROUTES:
        router.add_api_route(path, _eval_runs_disabled, methods=[method], include_in_schema=False)
    return router
