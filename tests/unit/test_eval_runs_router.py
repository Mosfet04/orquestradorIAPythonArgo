"""F3-08: as rotas ``/eval-runs*`` do app são só as do 404, com o mesmo método e path das do agno.

O comportamento (404 e nenhum egress de telemetria) está em ``tests/security/test_agno_telemetry.py``.
"""

from __future__ import annotations

from agno.agent import Agent
from agno.os.routers.evals import get_eval_router
from fastapi.routing import APIRoute

from src.infrastructure.runtime.agno.eval_runs_router import EVAL_RUNS_ROUTES
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import FakeChatModel
from tests.fakes.session_db import fake_session_db
from tests.fakes.web import mount_agent_os


def _method_paths(routes: list[object]) -> list[tuple[str, str]]:
    return sorted(
        (method, route.path)
        for route in routes
        if isinstance(route, APIRoute) and route.path.startswith("/eval-runs")
        for method in route.methods
    )


def test_cobre_toda_rota_de_evals_do_agno_instalado():
    """Rota de eval nova numa versão do agno passaria sem o 404: este teste quebra antes."""
    agno_routes = _method_paths(list(get_eval_router(dbs={}).routes))

    assert agno_routes == sorted(EVAL_RUNS_ROUTES)


def test_app_montado_so_tem_as_rotas_do_404_em_eval_runs():
    """O ``get_app()`` pulou as do agno (``preserve_base_app``): uma rota por método e path, fora do OpenAPI."""
    factory = AppFactory()
    app = factory.create_app()
    agent = Agent(id="ag", name="ag", model=FakeChatModel(responses=[]), telemetry=False, db=fake_session_db())

    mount_agent_os(factory, app, [agent], [])

    assert _method_paths(list(app.routes)) == sorted(EVAL_RUNS_ROUTES)
    assert not any(path.startswith("/eval-runs") for path in app.openapi()["paths"])
