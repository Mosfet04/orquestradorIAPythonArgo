"""F4-03: o evento de auditoria de tool call nunca leva segredo, valor de header, valor de argumento,
corpo de resposta nem texto de exceção. Dos argumentos, só os nomes declarados no schema e a contagem dos outros.

Upstream e servidor MCP são falsos (nada sai da máquina). Os valores são marcadores de vazamento, não segredos.
"""

from __future__ import annotations

import logging

import httpx
import pytest
from mcp.types import CallToolResult, TextContent

from src.domain.entities.tool import HttpMethod, ParameterType, Tool, ToolParameter
from src.infrastructure.logging import setup_structlog
from src.infrastructure.logging.structlog_logger import sanitize_log_data
from src.infrastructure.mcp_client.sessions import McpSessions, schema_hash
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from src.infrastructure.runtime.agno.mcp_tool_factory import McpToolFactory
from tests.fakes.logger import LoggedRecord, RecordingLogger
from tests.fakes.mcp_server import FAKE_MCP_URL, FakeMcpServer, mcp_tool

HEADER_SECRET = "valor-do-header-resolvido-f403"  # noqa: S105 - marcador de vazamento
ARGUMENT_VALUE = "cpf-123.456.789-09-f403"
BODY_TEXT = "corpo-da-resposta-f403"
EXCEPTION_TEXT = "texto-da-excecao-f403"
UNDECLARED_NAME = "nome-inventado-pelo-modelo-f403"
MARKERS = (HEADER_SECRET, ARGUMENT_VALUE, BODY_TEXT, EXCEPTION_TEXT, UNDECLARED_NAME)

SEARCH = mcp_tool("search", "Busca issues", {"q": {"type": "string"}})


def _audit(logger: RecordingLogger) -> list[LoggedRecord]:
    events = [r for r in logger.records if r.context.get("audit") == "tool_call"]
    assert events, "nenhum evento de auditoria"
    return events


def _assert_clean(events: list[LoggedRecord]) -> None:
    dumped = repr([(e.message, e.context) for e in events])
    for marker in MARKERS:
        assert marker not in dumped


def _http_tool() -> Tool:
    return Tool(
        id="cadastro",
        name="cadastro",
        description="Consulta cadastro",
        route="https://api.example.com/cadastro",
        http_method=HttpMethod.POST,
        parameters=[ToolParameter(name="cpf", type=ParameterType.STRING, description="CPF", required=True)],
        headers={"Authorization": "env:CADASTRO_API_KEY", "X-Fixo": HEADER_SECRET},
    )


@pytest.mark.parametrize("outcome", ["ok", "http_error", "exception", "refused"])
async def test_evento_http_nao_leva_header_argumento_corpo_nem_excecao(monkeypatch, outcome: str):
    monkeypatch.setenv("CADASTRO_API_KEY", HEADER_SECRET)
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        if outcome == "exception":
            raise httpx.ConnectError(f"{EXCEPTION_TEXT} {request.headers['authorization']}")
        return httpx.Response(200 if outcome == "ok" else 500, text=f"{BODY_TEXT} {ARGUMENT_VALUE}")

    logger = RecordingLogger()
    factory = HttpToolFactory(logger, transport=httpx.MockTransport(handler), retries=0)
    [function] = await factory.create_tools_from_configs([_http_tool()])
    assert function.entrypoint is not None
    arguments = {"cpf": ARGUMENT_VALUE}
    if outcome == "refused":
        arguments[UNDECLARED_NAME] = ARGUMENT_VALUE
    try:
        await function.entrypoint(**arguments)
    finally:
        await factory.aclose()

    assert (seen == []) is (outcome == "refused")
    [event] = _audit(logger)
    _assert_clean([event])
    assert event.context["arguments"] == ["cpf"]
    assert event.context["undeclared_arguments"] == (1 if outcome == "refused" else 0)


@pytest.mark.parametrize("is_error", [False, True])
async def test_evento_mcp_nao_leva_argumento_nem_resultado(is_error: bool):
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
    async with FakeMcpServer([SEARCH]) as server:
        server.results["search"] = CallToolResult(
            content=[TextContent(type="text", text=f"{BODY_TEXT} {ARGUMENT_VALUE}")], isError=is_error
        )
        sessions = McpSessions(logger, transport_factory=server.transport)
        try:
            [function] = await McpToolFactory(sessions, logger).create_tools_from_configs([tool])
            assert function.entrypoint is not None
            result = await function.entrypoint(q=ARGUMENT_VALUE, **{UNDECLARED_NAME: ARGUMENT_VALUE})
        finally:
            await sessions.aclose()

    assert BODY_TEXT in result  # o modelo recebe o resultado; o log de auditoria não
    [event] = _audit(logger)
    _assert_clean([event])
    assert (event.context["arguments"], event.context["undeclared_arguments"]) == (["q"], 1)


def test_sanitizador_de_producao_mascara_a_sessao_e_preserva_os_campos_da_auditoria():
    """No structlog, ``session_id`` sai como hash (campo sensível do sanitizador); o resto do evento passa inteiro."""
    event = {
        "event": "Auditoria de tool call",
        "audit": "tool_call",
        "source": "http",
        "tool_id": "cadastro",
        "function": "cadastro",
        "result": "ok",
        "duration_ms": 1.5,
        "arguments": ["cpf"],
        "undeclared_arguments": 0,
        "agent_id": "dev",
        "user_id": "ana",
        "run_id": "run-1",
        "session_id": "sess-secreta",
    }
    sanitized = sanitize_log_data(None, "info", dict(event))

    assert sanitized["session_id"].startswith("***MASKED***[hash:")
    assert "sess-secreta" not in repr(sanitized)
    assert {k: v for k, v in sanitized.items() if k != "session_id"} == {
        k: v for k, v in event.items() if k != "session_id"
    }


async def test_log_do_httpx_nao_leva_a_url_com_os_argumentos_da_tool(monkeypatch, tmp_path, caplog):
    """O logger ``httpx`` (INFO) registra a URL inteira do request, com a query montada dos argumentos do modelo
    (observação 3 da QA do F4-01). Com o logging do app, ele só passa de WARNING; o registro da chamada é o evento
    de auditoria, sem valores."""
    httpx_logger = logging.getLogger("httpx")
    monkeypatch.setattr(httpx_logger, "level", httpx_logger.level)  # restaura o nível depois do teste
    monkeypatch.chdir(tmp_path)  # setup_structlog cria logs/ no diretório corrente
    setup_structlog()
    caplog.set_level(logging.DEBUG)
    logger = RecordingLogger()
    factory = HttpToolFactory(logger, transport=httpx.MockTransport(lambda req: httpx.Response(200, json={})))
    tool = Tool(
        id="cadastro", name="cadastro", description="Consulta cadastro", route="https://api.example.com/cadastro",
        http_method=HttpMethod.GET,
        parameters=[ToolParameter(name="cpf", type=ParameterType.STRING, description="CPF", required=True)],
    )
    [function] = await factory.create_tools_from_configs([tool])
    assert function.entrypoint is not None
    try:
        await function.entrypoint(cpf=ARGUMENT_VALUE)
    finally:
        await factory.aclose()

    assert ARGUMENT_VALUE not in caplog.text
    [event] = _audit(logger)
    assert event.context["result"] == "ok"


AUDIT_IDENTITY_FIELDS = ("tool_id", "function", "mcp_tool", "run_id", "agent_id", "team_id")
AWS_SHAPED = "consultaCadastroClientesPessoaFisicaV2Interno"  # 40+ alfanuméricos: casa o padrão aws_secret_key
CARD_SHAPED = "12345678-1234-4234-8234-123456789012"  # uuid4 com 16 dígitos seguidos: casa o padrão credit_card


@pytest.mark.parametrize("value", [AWS_SHAPED, CARD_SHAPED])
def test_sanitizador_preserva_a_identificacao_no_evento_de_auditoria_e_mascara_a_sessao(value: str):
    """BUG-F4-03-A/B: no evento de auditoria, os campos de identificação passam inteiros; ``session_id`` não."""
    event = {"event": "Auditoria de tool call", "audit": "tool_call", "session_id": value}
    event.update(dict.fromkeys(AUDIT_IDENTITY_FIELDS, value))

    sanitized = sanitize_log_data(None, "info", dict(event))

    assert {key: sanitized[key] for key in AUDIT_IDENTITY_FIELDS} == dict.fromkeys(AUDIT_IDENTITY_FIELDS, value)
    assert sanitized["session_id"].startswith("***MASKED***[hash:") and value not in sanitized["session_id"]


@pytest.mark.parametrize("value", [AWS_SHAPED, CARD_SHAPED])
@pytest.mark.parametrize("marker", [None, "outro"])
def test_sanitizador_continua_mascarando_as_mesmas_chaves_fora_do_evento_de_auditoria(value: str, marker):
    """A proteção vale só para ``audit="tool_call"``: em outro log (``run_id`` do path do cancelamento,
    ``agent_id`` do path da API...) o valor é livre e o padrão de segredo continua mascarando."""
    event: dict[str, object] = {"event": "Erro ao buscar agente", **dict.fromkeys(AUDIT_IDENTITY_FIELDS, value)}
    if marker is not None:
        event["audit"] = marker

    sanitized = sanitize_log_data(None, "info", event)

    assert all(value not in str(sanitized[key]) for key in AUDIT_IDENTITY_FIELDS)
    assert all("_MASKED***" in str(sanitized[key]) for key in AUDIT_IDENTITY_FIELDS)


@pytest.mark.parametrize(
    "value",
    [
        "password=abc123secret",  # padrão password
        "abcdEFGH/ijklMNOP+qrstUVWX0123456789abcdWXYZ",  # 44 caracteres com / e +: padrão aws_secret_key
        "4111 1111 1111 1111",  # cartão com espaços: padrão credit_card
    ],
)
@pytest.mark.parametrize("key", AUDIT_IDENTITY_FIELDS)
def test_sanitizador_mascara_identificacao_fora_do_charset_no_evento_de_auditoria(key: str, value: str):
    """R5: ``tool_id``/``function`` são texto livre na config e o ``run_id`` do ``POST /agents/{id}/runs`` do
    AgentOS é escolhido pelo cliente; só valor ``[A-Za-z0-9_-]{1,64}`` passa inteiro, o resto segue a sanitização."""
    sanitized = sanitize_log_data(None, "info", {"event": "Auditoria de tool call", "audit": "tool_call", key: value})

    assert value not in str(sanitized[key])
    assert "_MASKED***" in str(sanitized[key])
