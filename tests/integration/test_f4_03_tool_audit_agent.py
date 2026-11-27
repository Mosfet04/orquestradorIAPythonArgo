"""F4-03 ponta a ponta: o agente montado chama uma tool HTTP e uma MCP; o agno injeta a identidade do run.

Cada chamada gera um evento de auditoria com o agente, o usuário, o run e a sessão do ``arun``.
Modelo roteirizado, upstream HTTP e servidor MCP falsos (nada sai da máquina).
"""

from __future__ import annotations

import json

import httpx
from agno.models.response import ModelResponse

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.model_config import ModelConfig
from src.domain.entities.tool import HttpMethod, Tool
from src.infrastructure.mcp_client.sessions import McpSessions, schema_hash
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from src.infrastructure.runtime.agno.mcp_tool_factory import McpToolFactory, ToolFactoryBySource
from tests.fakes import FakeEmbedderFactory, InMemoryToolRepository, RecordingLogger
from tests.fakes.mcp_server import FAKE_MCP_URL, FakeMcpServer, mcp_tool
from tests.fakes.models import FakeChatModel

SEARCH = mcp_tool("search", "Busca issues", {"q": {"type": "string"}})


class _ModelFactory:
    def __init__(self, model: FakeChatModel) -> None:
        self._model = model

    def create_model(self, config: ModelConfig) -> FakeChatModel:
        return self._model


def _calls(*calls: tuple[str, dict[str, object]]) -> ModelResponse:
    return ModelResponse(
        role="assistant",
        tool_calls=[
            {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}
            for i, (name, arguments) in enumerate(calls)
        ],
    )


async def _run(calls: ModelResponse, logger: RecordingLogger) -> str:
    """Monta o agente ``dev`` com as tools ``cep`` (HTTP) e ``github`` (MCP), roda um turno e devolve o run_id."""
    http_tool = Tool(
        id="cep", name="cep", description="Busca CEP", route="https://api.example.com/cep",
        http_method=HttpMethod.GET, parameters=[],
    )
    mcp = Tool(
        id="github", name="GitHub", description="Servidor MCP", route=FAKE_MCP_URL, http_method=HttpMethod.GET,
        parameters=[], mcp_tools={"search": schema_hash(SEARCH.name, SEARCH.description, SEARCH.inputSchema)},
    )
    model = FakeChatModel(responses=[calls, lambda text: ModelResponse(role="assistant", content="pronto")])
    http = HttpToolFactory(logger, transport=httpx.MockTransport(lambda req: httpx.Response(200, json={"uf": "SP"})))
    async with FakeMcpServer([SEARCH]) as server:
        sessions = McpSessions(logger, transport_factory=server.transport)
        service = AgentFactoryService(
            db_url="mongodb://mongo.invalid:27017",
            session_db=None,
            logger=logger,
            model_factory=_ModelFactory(model),  # type: ignore[arg-type]
            embedder_factory=FakeEmbedderFactory(),
            tool_factory=ToolFactoryBySource(http=http, mcp=McpToolFactory(sessions, logger)),
            tool_repository=InMemoryToolRepository([http_tool, mcp]),
        )
        config = AgentConfig(
            id="dev", nome="Dev", factory_ia_model="fake", model="m", descricao="d", prompt="p",
            tools_ids=["cep", "github"],
        )
        try:
            agent = await service.create_agent(config)
            output = await agent.arun("cep e bugs?", user_id="ana", session_id="sess-1")
        finally:
            await sessions.aclose()
            await http.aclose()
    assert output.run_id is not None
    return output.run_id


def _events(logger: RecordingLogger) -> list[dict[str, object]]:
    return [r.context for r in logger.records if r.context.get("audit") == "tool_call"]


async def test_cada_tool_call_do_agente_gera_evento_com_agente_usuario_run_e_sessao():
    logger = RecordingLogger()
    run_id = await _run(_calls(("cep", {}), ("github__search", {"q": "bug"})), logger)

    events = _events(logger)
    assert sorted((e["source"], e["function"], e["result"]) for e in events) == [
        ("http", "cep", "ok"),
        ("mcp", "github__search", "ok"),
    ]
    identity = {tuple(e[k] for k in ("agent_id", "user_id", "run_id", "session_id")) for e in events}
    assert identity == {("dev", "ana", run_id, "sess-1")}


async def test_modelo_nao_troca_a_identidade_do_evento_pelos_argumentos():
    """Argumento com o nome de um parâmetro injetado é descartado pelo agno; o evento fica com o agente real."""
    logger = RecordingLogger()
    forged = {"_agno_agent": {"id": "admin"}, "_agno_run_context": {"user_id": "root"}}
    run_id = await _run(_calls(("cep", forged), ("github__search", {"q": "x", **forged})), logger)

    events = _events(logger)
    assert [(e["agent_id"], e["user_id"], e["run_id"]) for e in events] == [("dev", "ana", run_id)] * 2
    assert {e["undeclared_arguments"] for e in events} == {0}
