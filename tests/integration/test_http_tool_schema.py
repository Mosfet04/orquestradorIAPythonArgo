"""Tools HTTP com Agno real (F1-05): o que chega ao modelo e o que é executado.

O modelo é o ``FakeChatModel``: ele registra as tools exatamente como o agno 2.5.8 as
entrega ao provider (``{"type": "function", "function": {...}}``) e as mensagens do run
(inclui o system message). A requisição HTTP vai para um ``httpx.MockTransport``: nada
sai da máquina.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.run.base import RunStatus

from src.domain.entities.tool import HttpMethod, ParameterType, Tool, ToolParameter
from src.infrastructure.http.http_tool_factory import HttpToolFactory
from tests.fakes import FakeChatModel, RecordingLogger

TODOS_OS_TIPOS = Tool(
    id="todos-os-tipos",
    name="Todos os tipos",
    description="Consulta pedidos de um cliente",
    route="https://api.example.invalid/clientes/{cliente_id}/pedidos",
    http_method=HttpMethod.POST,
    parameters=[
        ToolParameter(name="cliente_id", type=ParameterType.STRING, description="ID do cliente", required=True),
        ToolParameter(name="limite", type=ParameterType.INTEGER, description="Máximo de pedidos"),
        ToolParameter(name="valor_minimo", type=ParameterType.FLOAT, description="Valor mínimo", required=True),
        ToolParameter(name="abertos", type=ParameterType.BOOLEAN, description="Só pedidos abertos"),
        ToolParameter(name="filtro", type=ParameterType.OBJECT, description="Filtro livre"),
        ToolParameter(name="status", type=ParameterType.ARRAY, description="Status aceitos"),
    ],
    instructions="Use só quando o usuário informar o ID do cliente.",
)

SEM_INSTRUCOES = Tool(
    id="sem-instrucoes",
    name="Sem instruções",
    description="Busca endereço pelo CEP",
    route="https://api.example.invalid/cep/{cep}",
    http_method=HttpMethod.GET,
    parameters=[ToolParameter(name="cep", type=ParameterType.STRING, description="CEP", required=True)],
    instructions="",
)


async def _functions(*tools: Tool) -> list[Any]:
    logger = RecordingLogger()
    created = await HttpToolFactory(logger=logger).create_tools_from_configs(list(tools))
    assert logger.messages("error") == []
    return created


def _tool_dict(model: FakeChatModel, name: str) -> dict[str, Any]:
    sent = [t for t in model.calls[0].tools if t["function"]["name"] == name]
    assert len(sent) == 1, f"tool {name} não chegou ao modelo: {model.calls[0].tool_names}"
    return sent[0]


def _system_message(model: FakeChatModel) -> str:
    return next(content for role, content in model.calls[0].messages if role == "system")


async def test_schema_que_chega_ao_modelo_tem_nomes_tipos_required_e_descricoes():
    model = FakeChatModel(responses=["ok"])
    agent = Agent(id="a1", model=model, tools=await _functions(TODOS_OS_TIPOS), telemetry=False)

    await agent.arun("quais os pedidos?")

    assert _tool_dict(model, "todos-os-tipos") == {
        "type": "function",
        "function": {
            "name": "todos-os-tipos",
            "description": "Consulta pedidos de um cliente",
            "parameters": {
                "type": "object",
                "properties": {
                    "cliente_id": {"type": "string", "description": "ID do cliente"},
                    "limite": {"type": "integer", "description": "Máximo de pedidos"},
                    "valor_minimo": {"type": "number", "description": "Valor mínimo"},
                    "abertos": {"type": "boolean", "description": "Só pedidos abertos"},
                    "filtro": {"type": "object", "description": "Filtro livre"},
                    "status": {"type": "array", "items": {}, "description": "Status aceitos"},
                },
                "required": ["cliente_id", "valor_minimo"],
                "additionalProperties": False,
            },
        },
    }


async def test_tool_sem_parametros_chega_como_objeto_vazio():
    tool = Tool(
        id="ping",
        name="Ping",
        description="Verifica a API",
        route="https://api.example.invalid/ping",
        http_method=HttpMethod.GET,
        parameters=[],
    )
    model = FakeChatModel(responses=["ok"])
    agent = Agent(id="a1", model=model, tools=await _functions(tool), telemetry=False)

    await agent.arun("ping")

    assert _tool_dict(model, "ping")["function"]["parameters"] == {
        "type": "object",
        "properties": {},
        "required": [],
        "additionalProperties": False,
    }


async def test_instrucoes_da_tool_vao_para_o_system_message():
    model = FakeChatModel(responses=["ok"])
    agent = Agent(
        id="a1",
        model=model,
        instructions="Você é o atendente.",
        tools=await _functions(TODOS_OS_TIPOS, SEM_INSTRUCOES),
        telemetry=False,
    )

    await agent.arun("oi")

    system = _system_message(model)
    assert "Você é o atendente." in system
    assert "Instruções da tool todos-os-tipos: Use só quando o usuário informar o ID do cliente." in system
    assert "sem-instrucoes" not in system
    assert "api.example.invalid" not in system  # rota interna não vai para o prompt


async def test_tool_sem_instrucoes_nao_adiciona_texto_ao_prompt():
    (function,) = await _functions(SEM_INSTRUCOES)

    assert function.instructions is None


async def test_run_sincrono_falha_com_erro_claro_em_vez_de_descartar_a_tool():
    """agno 2.5.8: ``Function`` com entrypoint async em ``Agent.run`` vira erro do run.

    (Um ``Toolkit`` só com função async passaria pela checagem do agno e sumiria do run
    síncrono sem aviso: era o comportamento antes da F1-05.)
    """
    model = FakeChatModel(responses=["ok"])
    agent = Agent(id="a1", model=model, tools=await _functions(SEM_INSTRUCOES), telemetry=False)

    output = agent.run("qual o endereço do CEP 01001-000?")

    assert output.status == RunStatus.error
    assert "arun" in str(output.content)
    assert model.calls == []


# ── execução pelo agno (arun) ───────────────────────────────────────


@pytest.fixture
def http_requests(monkeypatch: pytest.MonkeyPatch) -> list[httpx.Request]:
    """Troca o transporte do ``httpx.AsyncClient`` por um ``MockTransport`` que registra o request."""
    seen: list[httpx.Request] = []
    real_client: Callable[..., httpx.AsyncClient] = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, json={"logradouro": "Praça da Sé"})

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return seen


def _tool_call(name: str, arguments: dict[str, Any]) -> ModelResponse:
    return ModelResponse(
        role="assistant",
        tool_calls=[
            {
                "id": "call-1",
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(arguments)},
            }
        ],
    )


async def test_arun_executa_a_tool_async_com_os_argumentos_do_schema(http_requests: list[httpx.Request]):
    model = FakeChatModel(responses=[_tool_call("sem-instrucoes", {"cep": "01001-000"}), "Praça da Sé"])
    agent = Agent(id="a1", model=model, tools=await _functions(SEM_INSTRUCOES), telemetry=False)

    output = await agent.arun("qual o endereço do CEP 01001-000?")

    assert output.status == RunStatus.completed
    assert output.content == "Praça da Sé"
    assert [str(r.url) for r in http_requests] == ["https://api.example.invalid/cep/01001-000"]
    tool_result = [content for role, content in model.calls[1].messages if role == "tool"]
    assert tool_result == ["{'logradouro': 'Praça da Sé'}"]


async def test_parametro_de_rota_e_codificado_e_os_demais_vao_no_corpo(http_requests: list[httpx.Request]):
    model = FakeChatModel(
        responses=[
            _tool_call("todos-os-tipos", {"cliente_id": "../admin?x=1#y", "valor_minimo": 10.5, "abertos": True}),
            "feito",
        ]
    )
    agent = Agent(id="a1", model=model, tools=await _functions(TODOS_OS_TIPOS), telemetry=False)

    await agent.arun("pedidos do cliente")

    (request,) = http_requests
    assert request.method == "POST"
    assert request.url.raw_path == b"/clientes/..%2Fadmin%3Fx%3D1%23y/pedidos"
    assert json.loads(request.content) == {"valor_minimo": 10.5, "abertos": True}
