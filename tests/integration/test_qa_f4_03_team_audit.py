"""QA F4-03: o ``team_id`` do evento de auditoria vem do time que o agno injeta (``_agno_team``).

Time com modelo roteirizado e a tool HTTP montada pela fábrica real, no nível do time (``Team.tools``). Upstream
falso: nada sai da máquina.
"""

from __future__ import annotations

import json

import httpx
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.team import Team

from src.domain.entities.tool import HttpMethod, Tool
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from tests.fakes.logger import RecordingLogger
from tests.fakes.models import FakeChatModel


async def test_tool_no_nivel_do_time_gera_evento_com_team_id_usuario_run_e_sessao():
    logger = RecordingLogger()
    http = HttpToolFactory(logger, transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"uf": "SP"})))
    tool = Tool(
        id="cep", name="cep", description="Busca CEP", route="https://api.example.com/cep",
        http_method=HttpMethod.GET, parameters=[],
    )  # fmt: skip
    [function] = await http.create_tools_from_configs([tool])
    call = ModelResponse(
        role="assistant",
        tool_calls=[{"id": "c0", "type": "function", "function": {"name": "cep", "arguments": json.dumps({})}}],
    )
    model = FakeChatModel(responses=[call, ModelResponse(role="assistant", content="pronto")])
    member = Agent(id="membro", name="Membro", model=FakeChatModel(responses=["x"]), telemetry=False)
    team = Team(id="suporte", name="Suporte", members=[member], model=model, tools=[function], telemetry=False)
    try:
        output = await team.arun("cep?", user_id="ana", session_id="sess-9")
    finally:
        await http.aclose()

    [event] = [r.context for r in logger.records if r.context.get("audit") == "tool_call"]
    assert (event["team_id"], event["user_id"], event["run_id"], event["session_id"]) == (
        "suporte", "ana", output.run_id, "sess-9",
    )  # fmt: skip
    assert event["agent_id"] is None
    assert (event["source"], event["result"]) == ("http", "ok")
