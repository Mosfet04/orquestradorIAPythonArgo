"""QA do F1-05: tools HTTP de ponta a ponta com o AgentOS real.

Cadeia: ``InMemoryToolRepository`` -> ``AgentFactoryService`` -> ``HttpToolFactory`` ->
``Agent``/``Team`` do agno -> ``AppFactory._mount_agent_os`` -> ``POST /agents/{id}/runs`` com
a chave run. O modelo é o ``FakeChatModel`` roteirizando ``tool_call``; o upstream é um
``httpx.MockTransport`` (nada sai da máquina). As chaves são valores de teste, não segredos.

Os defeitos de produção encontrados pelo QA (BUG-F1-05-QA-1..4) foram corrigidos na
rodada 2 do F1-05; os testes que os reproduziam ficaram sem ``xfail``.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

import httpx
import pytest
from agno.agent import Agent
from agno.models.response import ModelResponse
from agno.team import Team
from agno.team.mode import TeamMode
from starlette.testclient import TestClient

from src.application.services import agent_factory_service
from src.application.services.agent_factory_service import AgentFactoryService
from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.tool import HttpMethod, ParameterType, Tool, ToolParameter
from src.domain.ports import IModelFactory
from src.infrastructure.http.http_tool_factory import HttpToolFactory
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import (
    FakeChatModel,
    FakeEmbedderFactory,
    InMemoryToolRepository,
    RecordingLogger,
)

RUN_KEY = "qa-run-key-" + "r" * 21
ADMIN_KEY = "qa-admin-key-" + "a" * 19
ALL_INTERFACES = "0.0.0" + ".0"  # bind recusado sem chaves: é o cenário sob teste
AUTH = {"Authorization": f"Bearer {RUN_KEY}"}
_ENV_NAMES = ("ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS", "APP_HOST", "API_KEY_RUN", "API_KEY_ADMIN")

BASE = "https://api.example.invalid"

CEP = Tool(
    id="cep",
    name="CEP",
    description="Busca endereço pelo CEP",
    route=f"{BASE}/cep/{{cep}}",
    http_method=HttpMethod.GET,
    parameters=[ToolParameter(name="cep", type=ParameterType.STRING, description="CEP", required=True)],
    instructions="Passe o CEP só com dígitos.",
)
PEDIDO = Tool(
    id="pedido",
    name="Pedido",
    description="Cria pedido para um cliente",
    route=f"{BASE}/clientes/{{cliente_id}}/pedidos",
    http_method=HttpMethod.POST,
    parameters=[
        ToolParameter(name="cliente_id", type=ParameterType.STRING, description="ID do cliente", required=True),
        ToolParameter(name="quantidade", type=ParameterType.INTEGER, description="Qtde", required=True),
        ToolParameter(name="itens", type=ParameterType.ARRAY, description="Itens"),
    ],
)
BUSCA = Tool(
    id="busca",
    name="Busca",
    description="Busca com filtros",
    route=f"{BASE}/busca",
    http_method=HttpMethod.GET,
    parameters=[
        ToolParameter(name="q", type=ParameterType.STRING, description="Termo", required=True),
        ToolParameter(name="pagina", type=ParameterType.INTEGER, description="Página"),
        ToolParameter(name="tags", type=ParameterType.ARRAY, description="Tags"),
    ],
)
REMOVE = Tool(
    id="remove",
    name="Remove",
    description="Remove um item",
    route=f"{BASE}/itens/{{item_id}}",
    http_method=HttpMethod.DELETE,
    parameters=[
        ToolParameter(name="item_id", type=ParameterType.STRING, description="Item", required=True),
        ToolParameter(name="motivo", type=ParameterType.STRING, description="Motivo"),
    ],
)


def _tool_call(name: str, arguments: dict[str, Any] | str, call_id: str = "call-1") -> ModelResponse:
    raw = arguments if isinstance(arguments, str) else json.dumps(arguments)
    return ModelResponse(
        role="assistant",
        tool_calls=[{"id": call_id, "type": "function", "function": {"name": name, "arguments": raw}}],
    )


class _ScriptedModelFactory(IModelFactory):
    """Um ``FakeChatModel`` roteirizado por agente (chave = ``model_id`` do ``AgentConfig``)."""

    def __init__(self, scripts: dict[str, list[str | ModelResponse]]) -> None:
        self.models = {key: FakeChatModel(id=key, responses=list(script)) for key, script in scripts.items()}

    def create_model(self, factory_ia_model: str, model_id: str, **kwargs: Any) -> FakeChatModel:
        return self.models[model_id]

    def validate_model_config(self, factory_ia_model: str, model_id: str) -> dict[str, Any]:
        return {"valid": True, "factory_type": factory_ia_model, "model_id": model_id, "errors": []}


@pytest.fixture(autouse=True)
def no_mongo(monkeypatch: pytest.MonkeyPatch) -> None:
    """O ``MongoDb`` do agno criaria um ``MongoClient`` real; o agente funciona sem db."""
    monkeypatch.setattr(agent_factory_service, "MongoAgentDb", lambda **_: None)


Handler = Callable[[httpx.Request], httpx.Response]


class Upstream:
    """Troca o transporte do ``httpx.AsyncClient`` por um ``MockTransport`` que registra os requests."""

    def __init__(self) -> None:
        self.requests: list[httpx.Request] = []
        self.handler: Handler = lambda request: httpx.Response(200, json={"ok": True})


@pytest.fixture
def upstream(monkeypatch: pytest.MonkeyPatch) -> Upstream:
    state = Upstream()
    real_client: Callable[..., httpx.AsyncClient] = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        state.requests.append(request)
        return state.handler(request)

    def client(**kwargs: Any) -> httpx.AsyncClient:
        return real_client(transport=httpx.MockTransport(handler), **kwargs)

    monkeypatch.setattr(httpx, "AsyncClient", client)
    return state


def _config(agent_id: str, *tools_ids: str) -> AgentConfig:
    return AgentConfig(
        id=agent_id,
        nome=agent_id,
        factory_ia_model="fake",
        model=agent_id,
        descricao="desc",
        prompt="Você é um agente de teste.",
        tools_ids=list(tools_ids),
    )


async def _build_agent(
    logger: RecordingLogger,
    models: _ScriptedModelFactory,
    agent_id: str,
    tools: list[Tool],
    *tools_ids: str,
) -> Agent:
    service = AgentFactoryService(
        db_url="mongodb://mongo.invalid:27017",
        logger=logger,
        model_factory=models,
        embedder_factory=FakeEmbedderFactory(),
        tool_factory=HttpToolFactory(logger=logger),
        tool_repository=InMemoryToolRepository(tools),
    )
    return await service.create_agent(_config(agent_id, *tools_ids))


def _app(monkeypatch: pytest.MonkeyPatch, agents: list[Agent], teams: list[Team] | None = None) -> TestClient:
    for name in _ENV_NAMES:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    monkeypatch.setenv("APP_HOST", ALL_INTERFACES)
    monkeypatch.setenv("ENVIRONMENT", "production")
    monkeypatch.setenv("API_KEY_RUN", RUN_KEY)
    monkeypatch.setenv("API_KEY_ADMIN", ADMIN_KEY)
    factory = AppFactory()
    app = factory.create_app()
    factory._mount_agent_os(app, agents, teams or [])
    return TestClient(app, raise_server_exceptions=False)


def _run(client: TestClient, agent_id: str, message: str = "oi") -> dict[str, Any]:
    response = client.post(f"/agents/{agent_id}/runs", data={"message": message, "stream": "false"}, headers=AUTH)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


def _tool_results(model: FakeChatModel, call_index: int) -> list[str]:
    return [content for role, content in model.calls[call_index].messages if role == "tool"]


# ── agente com 2 tools pelo AgentOS real ─────────────────────────────


async def test_agente_com_duas_tools_pelo_agentos_executa_requests_corretos_e_devolve_resultado_ao_modelo(
    monkeypatch: pytest.MonkeyPatch, upstream: Upstream
):
    logger = RecordingLogger()
    models = _ScriptedModelFactory(
        {
            "a1": [
                _tool_call("cep", {"cep": "01001000"}, "c1"),
                _tool_call("pedido", {"cliente_id": "42", "quantidade": 3, "itens": ["x", "y"]}, "c2"),
                "pedido criado",
            ]
        }
    )
    agent = await _build_agent(logger, models, "a1", [CEP, PEDIDO], "cep", "pedido")

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.startswith("/cep/"):
            return httpx.Response(200, json={"logradouro": "Praça da Sé"})
        return httpx.Response(201, json={"id": 99})

    upstream.handler = handler
    client = _app(monkeypatch, [agent])

    body = _run(client, "a1", "crie o pedido do cliente 42 no CEP 01001000")

    assert body["content"] == "pedido criado"
    methods_urls = [(r.method, str(r.url)) for r in upstream.requests]
    assert methods_urls == [
        ("GET", f"{BASE}/cep/01001000"),
        ("POST", f"{BASE}/clientes/42/pedidos"),
    ]
    assert json.loads(upstream.requests[1].content) == {"quantidade": 3, "itens": ["x", "y"]}
    assert upstream.requests[0].content == b""
    # o resultado volta ao modelo na chamada seguinte
    model = models.models["a1"]
    assert _tool_results(model, 1) == ["{'logradouro': 'Praça da Sé'}"]
    assert "{'id': 99}" in _tool_results(model, 2)
    # o schema e as instruções chegaram ao modelo na primeira chamada
    sent = {t["function"]["name"]: t["function"] for t in model.calls[0].tools}
    assert set(sent) == {"cep", "pedido"}
    assert sent["pedido"]["parameters"]["required"] == ["cliente_id", "quantidade"]
    assert sent["pedido"]["parameters"]["properties"]["quantidade"]["type"] == "integer"
    system = next(c for role, c in model.calls[0].messages if role == "system")
    assert "Instruções da tool cep: Passe o CEP só com dígitos." in system
    assert logger.messages("error") == []


async def test_agente_com_tool_pelo_agentos_com_stream_sse_executa_a_tool(
    monkeypatch: pytest.MonkeyPatch, upstream: Upstream
):
    models = _ScriptedModelFactory({"a1": [_tool_call("cep", {"cep": "01001000"}), "pronto"]})
    agent = await _build_agent(RecordingLogger(), models, "a1", [CEP], "cep")
    client = _app(monkeypatch, [agent])

    response = client.post("/agents/a1/runs", data={"message": "oi", "stream": "true"}, headers=AUTH)

    assert response.status_code == 200
    events = [line.removeprefix("event: ") for line in response.text.splitlines() if line.startswith("event: ")]
    assert "ToolCallStarted" in events and "ToolCallCompleted" in events
    assert events[-1] == "RunCompleted" and "RunError" not in events
    assert [str(r.url) for r in upstream.requests] == [f"{BASE}/cep/01001000"]


async def test_duas_runs_seguidas_usam_a_mesma_tool_sem_vazar_argumentos(
    monkeypatch: pytest.MonkeyPatch, upstream: Upstream
):
    models = _ScriptedModelFactory(
        {
            "a1": [
                _tool_call("cep", {"cep": "11111111"}),
                "um",
                _tool_call("cep", {"cep": "22222222"}),
                "dois",
            ]
        }
    )
    agent = await _build_agent(RecordingLogger(), models, "a1", [CEP], "cep")
    client = _app(monkeypatch, [agent])

    assert _run(client, "a1")["content"] == "um"
    assert _run(client, "a1")["content"] == "dois"

    assert [str(r.url) for r in upstream.requests] == [f"{BASE}/cep/11111111", f"{BASE}/cep/22222222"]


async def test_dois_agentes_cada_um_com_sua_tool_nao_misturam_tools(
    monkeypatch: pytest.MonkeyPatch, upstream: Upstream
):
    models = _ScriptedModelFactory(
        {"a1": [_tool_call("cep", {"cep": "1"}), "ok1"], "a2": [_tool_call("busca", {"q": "x"}), "ok2"]}
    )
    a1 = await _build_agent(RecordingLogger(), models, "a1", [CEP, BUSCA], "cep")
    a2 = await _build_agent(RecordingLogger(), models, "a2", [CEP, BUSCA], "busca")
    client = _app(monkeypatch, [a1, a2])

    _run(client, "a1")
    _run(client, "a2")

    assert [t["function"]["name"] for t in models.models["a1"].calls[0].tools] == ["cep"]
    assert [t["function"]["name"] for t in models.models["a2"].calls[0].tools] == ["busca"]


# ── team com membro que tem tool ─────────────────────────────────────


async def test_team_delega_ao_membro_que_tem_tool_e_o_request_http_sai(
    monkeypatch: pytest.MonkeyPatch, upstream: Upstream
):
    models = _ScriptedModelFactory(
        {
            "membro": [_tool_call("cep", {"cep": "01001000"}), "Praça da Sé"],
            "time": [
                _tool_call("delegate_task_to_member", {"member_id": "membro", "task": "busque o CEP 01001000"}),
                "O endereço é Praça da Sé",
            ],
        }
    )
    member = await _build_agent(RecordingLogger(), models, "membro", [CEP], "cep")
    team = Team(
        id="time",
        name="Time",
        mode=TeamMode.coordinate,
        model=models.models["time"],
        members=[member],
        telemetry=False,
    )
    client = _app(monkeypatch, [member], [team])

    response = client.post("/teams/time/runs", data={"message": "CEP 01001000?", "stream": "false"}, headers=AUTH)

    assert response.status_code == 200, response.text
    assert [(r.method, str(r.url)) for r in upstream.requests] == [("GET", f"{BASE}/cep/01001000")]
    assert _tool_results(models.models["membro"], 1) == ["{'ok': True}"]
    assert "Praça da Sé" in response.text


# ── comportamento de erro do upstream: o que volta ao modelo ─────────


@pytest.mark.parametrize(
    ("make_response", "expected_prefix", "log_context"),
    [
        (
            lambda r: httpx.Response(404, text="não achei"),
            "Erro HTTP 404: não achei",
            {"tool_id": "cep", "status": 404},
        ),
        (lambda r: httpx.Response(503, text="fora"), "Erro HTTP 503: fora", {"tool_id": "cep", "status": 503}),
        (
            lambda r: httpx.Response(302, headers={"location": "http://127.0.0.1/"}),
            "Erro HTTP 302",
            {"tool_id": "cep", "status": 302},
        ),
    ],
    ids=["4xx", "5xx", "redirect-nao-seguido"],
)
async def test_status_de_erro_do_upstream_volta_ao_modelo_como_texto_e_e_logado_sem_derrubar_o_run(
    monkeypatch: pytest.MonkeyPatch,
    upstream: Upstream,
    make_response: Handler,
    expected_prefix: str,
    log_context: dict[str, Any],
):
    logger = RecordingLogger()
    models = _ScriptedModelFactory({"a1": [_tool_call("cep", {"cep": "1"}), "não consegui"]})
    agent = await _build_agent(logger, models, "a1", [CEP], "cep")
    upstream.handler = make_response
    client = _app(monkeypatch, [agent])

    body = _run(client, "a1")

    assert body["content"] == "não consegui"
    (result,) = _tool_results(models.models["a1"], 1)
    assert result.startswith(expected_prefix)
    assert "Traceback" not in result
    assert [(r.message, r.context) for r in logger.records if r.level == "error"] == [("HTTP error", log_context)]
    assert len(upstream.requests) == 1  # redirect não é seguido: sem salto para outro host


async def test_falha_de_rede_do_upstream_volta_ao_modelo_sem_stack_e_e_logada(
    monkeypatch: pytest.MonkeyPatch, upstream: Upstream
):
    logger = RecordingLogger()
    models = _ScriptedModelFactory({"a1": [_tool_call("cep", {"cep": "1"}), "fora do ar"]})
    agent = await _build_agent(logger, models, "a1", [CEP], "cep")

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("conexão recusada", request=request)

    upstream.handler = boom
    client = _app(monkeypatch, [agent])

    body = _run(client, "a1")

    assert body["content"] == "fora do ar"
    assert _tool_results(models.models["a1"], 1) == ["Erro na requisição: conexão recusada"]
    assert [r.message for r in logger.records if r.level == "error"] == ["Request error"]


async def test_run_http_nao_vaza_traceback_nem_caminho_de_arquivo_quando_a_tool_falha(
    monkeypatch: pytest.MonkeyPatch, upstream: Upstream
):
    models = _ScriptedModelFactory({"a1": [_tool_call("cep", {"cep": "1"}), "fim"]})
    agent = await _build_agent(RecordingLogger(), models, "a1", [CEP], "cep")

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("lento", request=request)

    upstream.handler = boom
    client = _app(monkeypatch, [agent])

    response = client.post("/agents/a1/runs", data={"message": "oi", "stream": "false"}, headers=AUTH)

    assert response.status_code == 200
    assert "Traceback" not in response.text
    assert "/home/" not in response.text and "site-packages" not in response.text
    (result,) = _tool_results(models.models["a1"], 1)
    assert result.startswith("Erro na requisição")


async def test_corpo_cru_do_upstream_chega_ao_modelo_registro_s2_f4_01(
    monkeypatch: pytest.MonkeyPatch, upstream: Upstream
):
    """Registro (S2, escopo F4-01): o corpo de erro do upstream vai inteiro ao modelo.

    Caracteriza o comportamento atual; quando a F4-01 truncar/sanear, este teste muda.
    """
    models = _ScriptedModelFactory({"a1": [_tool_call("cep", {"cep": "1"}), "fim"]})
    agent = await _build_agent(RecordingLogger(), models, "a1", [CEP], "cep")
    upstream.handler = lambda r: httpx.Response(500, text="detalhe-interno " + "x" * 5000)
    client = _app(monkeypatch, [agent])

    _run(client, "a1")

    (result,) = _tool_results(models.models["a1"], 1)
    assert "detalhe-interno" in result and len(result) > 5000


# ── GET/DELETE: parâmetros em query string; POST: corpo JSON ─────────


async def test_get_envia_parametros_na_query_string_e_sem_corpo(monkeypatch: pytest.MonkeyPatch, upstream: Upstream):
    models = _ScriptedModelFactory(
        {"a1": [_tool_call("busca", {"q": "a b&c=d", "pagina": 2, "tags": ["x", "y"]}), "ok"]}
    )
    agent = await _build_agent(RecordingLogger(), models, "a1", [BUSCA], "busca")
    client = _app(monkeypatch, [agent])

    _run(client, "a1")

    (request,) = upstream.requests
    assert request.method == "GET"
    assert request.content == b""
    assert request.url.path == "/busca"
    assert request.url.params.multi_items() == [("q", "a b&c=d"), ("pagina", "2"), ("tags", "x"), ("tags", "y")]


async def test_delete_com_parametro_de_rota_envia_o_restante_na_query_e_sem_corpo(
    monkeypatch: pytest.MonkeyPatch, upstream: Upstream
):
    models = _ScriptedModelFactory({"a1": [_tool_call("remove", {"item_id": "7", "motivo": "duplicado"}), "ok"]})
    agent = await _build_agent(RecordingLogger(), models, "a1", [REMOVE], "remove")
    client = _app(monkeypatch, [agent])

    _run(client, "a1")

    (request,) = upstream.requests
    assert (request.method, request.url.path) == ("DELETE", "/itens/7")
    assert request.url.params.multi_items() == [("motivo", "duplicado")]
    assert request.content == b""


# ── argumentos do tool_call que contrariam o schema ──────────────────


async def test_argumentos_nao_json_voltam_ao_modelo_como_erro_do_agno_sem_chamar_o_upstream(
    monkeypatch: pytest.MonkeyPatch, upstream: Upstream
):
    models = _ScriptedModelFactory({"a1": [_tool_call("cep", "{nao json"), "desisti"]})
    agent = await _build_agent(RecordingLogger(), models, "a1", [CEP], "cep")
    client = _app(monkeypatch, [agent])

    body = _run(client, "a1")

    assert body["content"] == "desisti"
    assert upstream.requests == []
    (result,) = _tool_results(models.models["a1"], 1)
    assert "Error while decoding function arguments" in result
    assert "Traceback" not in result


async def test_tool_inexistente_no_tool_call_volta_ao_modelo_como_erro_sem_request(
    monkeypatch: pytest.MonkeyPatch, upstream: Upstream
):
    models = _ScriptedModelFactory({"a1": [_tool_call("nao-existe", {}), "ok"]})
    agent = await _build_agent(RecordingLogger(), models, "a1", [CEP], "cep")
    client = _app(monkeypatch, [agent])

    body = _run(client, "a1")

    assert body["content"] == "ok"
    assert upstream.requests == []
    (result,) = _tool_results(models.models["a1"], 1)
    assert "does not exist" in result and "Traceback" not in result


async def test_parametro_de_rota_contendo_placeholder_nao_e_reexpandido(
    monkeypatch: pytest.MonkeyPatch, upstream: Upstream
):
    """Valor do modelo com ``{outro}``: o encode das chaves impede segunda substituição."""
    tool = Tool(
        id="duplo",
        name="Duplo",
        description="Dois segmentos",
        route=f"{BASE}/a/{{x}}/b/{{y}}",
        http_method=HttpMethod.GET,
        parameters=[
            ToolParameter(name="x", type=ParameterType.STRING, description="x", required=True),
            ToolParameter(name="y", type=ParameterType.STRING, description="y", required=True),
        ],
    )
    models = _ScriptedModelFactory({"a1": [_tool_call("duplo", {"x": "{y}", "y": "segredo"}), "ok"]})
    agent = await _build_agent(RecordingLogger(), models, "a1", [tool], "duplo")
    client = _app(monkeypatch, [agent])

    _run(client, "a1")

    (request,) = upstream.requests
    assert request.url.raw_path == b"/a/%7By%7D/b/segredo"


@pytest.mark.parametrize(
    "name", ["url", "method", "headers", "timeout", "params", "json", "verify", "follow_redirects"]
)
async def test_argumento_com_nome_de_opcao_do_httpx_nao_altera_destino_nem_metodo(
    monkeypatch: pytest.MonkeyPatch, upstream: Upstream, name: str
):
    """Argumento hostil (não declarado no schema) nunca vira opção do cliente HTTP.

    Rodada 2 do F1-05: argumento não declarado é recusado antes do request (nenhum
    request sai, para host nenhum) e o modelo recebe o erro com o nome do argumento.
    """
    models = _ScriptedModelFactory(
        {"a1": [_tool_call("cep", {"cep": "1", name: "http://evil.example.invalid/"}), "ok"]}
    )
    agent = await _build_agent(RecordingLogger(), models, "a1", [CEP], "cep")
    client = _app(monkeypatch, [agent])

    body = _run(client, "a1")

    assert body["content"] == "ok"
    assert upstream.requests == []
    (result,) = _tool_results(models.models["a1"], 1)
    assert result == f"Erro nos argumentos da tool cep: argumento não declarado: {name}"


# ── BUGS de produção encontrados pelo QA (corrigidos na rodada 2) ────


async def test_tool_call_sem_argumento_obrigatorio_nao_chama_o_upstream_e_o_modelo_recebe_erro_claro(
    monkeypatch: pytest.MonkeyPatch, upstream: Upstream
):
    models = _ScriptedModelFactory({"a1": [_tool_call("cep", {}), "ok"]})
    agent = await _build_agent(RecordingLogger(), models, "a1", [CEP], "cep")
    client = _app(monkeypatch, [agent])

    _run(client, "a1")

    assert upstream.requests == []
    (result,) = _tool_results(models.models["a1"], 1)
    assert "cep" in result and "obrigat" in result.lower()


async def test_tool_call_com_argumento_nao_declarado_nao_e_repassado_ao_upstream(
    monkeypatch: pytest.MonkeyPatch, upstream: Upstream
):
    models = _ScriptedModelFactory({"a1": [_tool_call("busca", {"q": "x", "admin": "true"}), "ok"]})
    agent = await _build_agent(RecordingLogger(), models, "a1", [BUSCA], "busca")
    client = _app(monkeypatch, [agent])

    _run(client, "a1")

    assert all("admin" not in r.url.params for r in upstream.requests)


async def test_tool_call_com_tipo_errado_nao_chama_o_upstream(monkeypatch: pytest.MonkeyPatch, upstream: Upstream):
    models = _ScriptedModelFactory({"a1": [_tool_call("busca", {"q": "x", "pagina": "abc"}), "ok"]})
    agent = await _build_agent(RecordingLogger(), models, "a1", [BUSCA], "busca")
    client = _app(monkeypatch, [agent])

    _run(client, "a1")

    assert upstream.requests == []


async def test_timeout_do_upstream_diz_ao_modelo_que_foi_timeout(monkeypatch: pytest.MonkeyPatch, upstream: Upstream):
    models = _ScriptedModelFactory({"a1": [_tool_call("cep", {"cep": "1"}), "fim"]})
    agent = await _build_agent(RecordingLogger(), models, "a1", [CEP], "cep")

    def boom(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("", request=request)

    upstream.handler = boom
    client = _app(monkeypatch, [agent])

    _run(client, "a1")

    (result,) = _tool_results(models.models["a1"], 1)
    assert "timeout" in result.lower() or "tempo" in result.lower()
