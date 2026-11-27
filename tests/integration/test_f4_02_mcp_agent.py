"""F4-02 ponta a ponta: agente montado do repositório com uma tool MCP, modelo roteirizado e servidor MCP falso.

O modelo só vê a tool da allowlist (com o schema do servidor) e o resultado da chamada volta ao contexto dele.
"""

from __future__ import annotations

import json

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


def _call(name: str, arguments: dict[str, object]) -> ModelResponse:
    return ModelResponse(
        role="assistant",
        tool_calls=[{"id": "c1", "type": "function", "function": {"name": name, "arguments": json.dumps(arguments)}}],
    )


async def test_agente_chama_a_tool_mcp_da_allowlist_e_recebe_o_resultado():
    logger = RecordingLogger()
    tool = Tool(
        id="github",
        name="GitHub",
        description="Servidor MCP",
        route=FAKE_MCP_URL,
        http_method=HttpMethod.GET,
        parameters=[],
        mcp_tools={"search": schema_hash(SEARCH.name, SEARCH.description, SEARCH.inputSchema)},
    )
    model = FakeChatModel(
        responses=[_call("github__search", {"q": "bug"}), lambda text: ModelResponse(role="assistant", content=text)]
    )
    async with FakeMcpServer([SEARCH, mcp_tool("delete_repo")]) as server:
        server.results["search"] = "3 issues abertas"
        sessions = McpSessions(logger, transport_factory=server.transport)
        service = AgentFactoryService(
            db_url="mongodb://mongo.invalid:27017",
            session_db=None,
            logger=logger,
            model_factory=_ModelFactory(model),  # type: ignore[arg-type]
            embedder_factory=FakeEmbedderFactory(),
            tool_factory=ToolFactoryBySource(http=HttpToolFactory(logger), mcp=McpToolFactory(sessions, logger)),
            tool_repository=InMemoryToolRepository([tool]),
        )
        config = AgentConfig(
            id="dev", nome="Dev", factory_ia_model="fake", model="m", descricao="d", prompt="p", tools_ids=["github"]
        )
        try:
            agent = await service.create_agent(config)
            output = await agent.arun("bugs abertos?")
        finally:
            await sessions.aclose()

    assert [call.tool_names for call in model.calls] == [("github__search",), ("github__search",)]
    assert server.calls == [("search", {"q": "bug"})]
    assert "3 issues abertas" in str(output.content)


def _mcp_tool_config(tool_id: str, *tools: object) -> Tool:
    return Tool(
        id=tool_id,
        name=tool_id,
        description="Servidor MCP",
        route=FAKE_MCP_URL,
        http_method=HttpMethod.GET,
        parameters=[],
        mcp_tools={t.name: schema_hash(t.name, t.description, t.inputSchema) for t in tools},  # type: ignore[attr-defined]
    )


async def test_nome_de_funcao_repetido_no_agente_recusa_a_tool_que_chega_depois_com_log():
    logger = RecordingLogger()
    b_c, c = mcp_tool("b__c"), mcp_tool("c")
    http = Tool(
        id="github__search", name="CEP", description="Busca CEP", route="https://api.example.com/cep",
        http_method=HttpMethod.GET, parameters=[],
    )
    repository = InMemoryToolRepository(
        [_mcp_tool_config("a", b_c), _mcp_tool_config("a__b", c), http, _mcp_tool_config("github", SEARCH)]
    )
    async with FakeMcpServer([b_c, c, SEARCH]) as server:
        sessions = McpSessions(logger, transport_factory=server.transport)
        service = AgentFactoryService(
            db_url="mongodb://mongo.invalid:27017",
            session_db=None,
            logger=logger,
            model_factory=_ModelFactory(FakeChatModel(responses=[])),  # type: ignore[arg-type]
            embedder_factory=FakeEmbedderFactory(),
            tool_factory=ToolFactoryBySource(http=HttpToolFactory(logger), mcp=McpToolFactory(sessions, logger)),
            tool_repository=repository,
        )
        config = AgentConfig(
            id="dev", nome="Dev", factory_ia_model="fake", model="m", descricao="d", prompt="p",
            tools_ids=["a", "a__b", "github__search", "github"],
        )
        try:
            agent = await service.create_agent(config)
        finally:
            await sessions.aclose()

    assert [(f.name, f.description) for f in agent.tools] == [  # type: ignore[union-attr]
        ("a__b__c", "Descrição"),
        ("github__search", "Busca CEP"),
    ]
    refused = [r.context for r in logger.records if r.message.startswith("Função de tool recusada")]
    assert refused == [
        {"agent_id": "dev", "tool_id": "a__b", "function": "a__b__c"},
        {"agent_id": "dev", "tool_id": "github", "function": "github__search"},
    ]
