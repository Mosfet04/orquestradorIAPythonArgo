"""FA-06, segurança: SSRF na ``route`` de tool gravada pela API e header por referência.

Borda real (CORS + auth) com repositórios em memória e a guarda de destino real sobre DNS falso
(``tests/fakes/admin_config_app.py``). Na chamada, o upstream é um ``httpx.MockTransport`` instrumentado pelo
OTel (spans em memória). Valores são de teste.
"""

from __future__ import annotations

import asyncio
from typing import Any

import httpx
import pytest
from opentelemetry.instrumentation.httpx import AsyncOpenTelemetryTransport
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.tool import HttpMethod, Tool
from src.infrastructure.providers import ClassSpec, DestinationPolicy, ProviderRegistry, ProviderSpec
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from tests.fakes.admin_config_app import ADMIN, admin_world, agent_body, route_guard, tool_body
from tests.fakes.http_upstream import RecordingUpstream
from tests.fakes.logger import RecordingLogger
from tests.fakes.providers import CHAT_PATH
from tests.fakes.runtime import FakeAgentHandle, FakeAgentRuntime

BASE = "/admin/config/tools"
INVALID_ROUTE = {"detail": "Configuração inválida", "field": "route"}
SECRET = "valor-de-teste-do-header-resolvido"  # noqa: S105 - marcador de vazamento, não é segredo


@pytest.mark.parametrize(
    "route",
    [
        "http://169.254.169.254/latest/meta-data/{cep}",
        "http://2852039166/{cep}",
        "http://[::ffff:169.254.169.254]/{cep}",
        "http://10.0.0.5/{cep}",
        "http://127.0.0.1:8080/{cep}",
        "http://{cep}/x",  # host escolhido pelo modelo
    ],
)
async def test_route_para_ip_literal_proibido_responde_422_sem_gravar_nem_ecoar(
    monkeypatch: pytest.MonkeyPatch, route: str
):
    async with admin_world(monkeypatch) as world:
        response = await world.client.post(BASE, json=tool_body(route=route), headers=ADMIN)

    assert (response.status_code, response.json()) == (422, INVALID_ROUTE)
    assert await world.tools.list_all() == []
    assert route not in repr(world.logger.records)


async def test_route_com_nome_que_resolve_para_ip_privado_responde_422(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch, guard=route_guard(resolver=lambda host: ["192.168.0.10"])) as world:
        created = await world.client.post(
            BASE, json=tool_body(route="https://intranet.example.invalid/{cep}"), headers=ADMIN
        )

    assert (created.status_code, created.json()) == (422, INVALID_ROUTE)
    assert await world.tools.list_all() == []


async def test_header_sensivel_em_texto_responde_422_e_o_valor_nao_volta(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch) as world:
        response = await world.client.post(BASE, json=tool_body(headers={"Authorization": SECRET}), headers=ADMIN)

    assert (response.status_code, response.json()) == (422, {"detail": "Configuração inválida", "field": "headers"})
    assert SECRET not in response.text and SECRET not in repr(world.logger.records)


def _traced_upstream(monkeypatch: pytest.MonkeyPatch) -> tuple[RecordingUpstream, InMemorySpanExporter]:
    """Upstream gravador com spans do OTel em memória (o cliente da tool segue redirect, como em produção)."""
    exporter = InMemorySpanExporter()
    provider = TracerProvider()
    provider.add_span_processor(SimpleSpanProcessor(exporter))

    def instrument(transport: httpx.AsyncBaseTransport) -> httpx.AsyncBaseTransport:
        return AsyncOpenTelemetryTransport(transport, tracer_provider=provider)

    return RecordingUpstream().install(monkeypatch, wrap=instrument, follow_redirects=True), exporter


async def test_header_por_referencia_resolve_na_chamada_e_nao_aparece_em_log_trace_nem_resposta(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CEP_API_KEY", SECRET)
    body = tool_body(route="https://api.interna.example/cep", headers={"Authorization": "env:CEP_API_KEY"})
    async with admin_world(monkeypatch, guard=route_guard("api.interna.example")) as world:
        created = await world.client.post(BASE, json=body, headers=ADMIN)
        listed = await world.client.get(BASE, headers=ADMIN)
        history = await world.client.get(f"{BASE}/cep/history", headers=ADMIN)
        [stored] = await world.tools.list_all()
    upstream, exporter = _traced_upstream(monkeypatch)  # depois do cliente da borda, que também é httpx
    upstream.handler = lambda request: httpx.Response(401, json={"erro": "não autorizado"})
    logger = RecordingLogger()
    [function] = await HttpToolFactory(logger).create_tools_from_configs([stored.entity])
    assert function.entrypoint is not None
    result = await function.entrypoint()

    assert created.status_code == 201
    assert created.json()["headers"] == {"Authorization": "env:CEP_API_KEY"}
    assert [r.headers["authorization"] for r in upstream.seen] == [SECRET]  # resolvido na chamada
    spans = exporter.get_finished_spans()
    assert spans  # o request foi rastreado
    leaks = [
        text
        for text in (
            created.text,
            listed.text,
            history.text,
            result,
            repr(world.logger.records),
            repr(logger.records),
            *(repr(dict(span.attributes or {})) + repr(span.events) for span in spans),
        )
        if SECRET in text
    ]
    assert leaks == []


async def test_redirect_do_upstream_para_metadata_nao_e_seguido(monkeypatch: pytest.MonkeyPatch):
    upstream, _ = _traced_upstream(monkeypatch)
    upstream.handler = lambda request: httpx.Response(
        302, headers={"Location": "http://169.254.169.254/latest/meta-data/"}
    )
    tool = Tool(
        id="cep",
        name="cep",
        description="Busca CEP",
        route="https://api.example.invalid/cep",
        http_method=HttpMethod.GET,
        parameters=[],
    )
    [function] = await HttpToolFactory(RecordingLogger()).create_tools_from_configs([tool])
    assert function.entrypoint is not None
    result = await function.entrypoint()

    assert [str(r.url) for r in upstream.seen] == ["https://api.example.invalid/cep"]
    assert result.startswith("Erro HTTP 302")


class _ModelGuardedRuntime(FakeAgentRuntime):
    """Cria o modelo pelo ``ProviderRegistry`` real (guarda de destino da ``base_url``) antes do handle falso.

    Como o ``AgnoRuntime``: a montagem do agente chama ``create_model`` fora do event loop.
    """

    def __init__(self, registry: ProviderRegistry) -> None:
        super().__init__()
        self._registry = registry

    async def build_agent(self, config: AgentConfig) -> FakeAgentHandle:
        await asyncio.to_thread(self._registry.create_model, config.model_config)
        return await super().build_agent(config)


@pytest.mark.parametrize(
    ("base_url", "dns"),
    [
        ("http://169.254.169.254/v1", "203.0.113.9"),  # literal: nem chega ao DNS
        ("http://gw.permitido.example/v1", "169.254.169.254"),  # host da allowlist que resolve para metadata
        ("http://fora.example/v1", "203.0.113.9"),  # fora do provider e da allowlist
    ],
)
async def test_base_url_de_modelo_passa_pela_guarda_na_escrita_do_agente(
    monkeypatch: pytest.MonkeyPatch, base_url: str, dns: str
):
    """A montagem antes de gravar (FA-04) é onde a ``base_url`` passa pela guarda: recusada, nada é gravado."""
    async with admin_world(monkeypatch, runtime=_registry_runtime(dns)) as world:
        response = await world.client.post("/admin/config/agents", json=_contract_agent(base_url), headers=ADMIN)

    assert response.status_code == 422
    assert response.json() == {"detail": "Configuração não monta no runtime", "error_type": "InvalidModelConfigError"}
    assert await world.agents.list_all() == []


async def test_base_url_da_allowlist_com_dns_publico_e_gravada(monkeypatch: pytest.MonkeyPatch):
    """Controle do teste acima: com destino permitido, a mesma escrita grava."""
    async with admin_world(monkeypatch, runtime=_registry_runtime("203.0.113.9")) as world:
        body = _contract_agent("http://gw.permitido.example/v1")
        response = await world.client.post("/admin/config/agents", json=body, headers=ADMIN)

    assert response.status_code == 201
    assert [i.entity.base_url for i in await world.agents.list_all()] == ["http://gw.permitido.example/v1"]


def _registry_runtime(dns: str) -> _ModelGuardedRuntime:
    spec = ProviderSpec(id="contrato", sdk_package="contrato-sdk", chat=ClassSpec(CHAT_PATH, base_url_kwarg="base_url"))
    policy = DestinationPolicy(allowlist=("gw.permitido.example",), resolver=lambda host: [dns])
    return _ModelGuardedRuntime(ProviderRegistry([spec], policy=policy))


def _contract_agent(base_url: str) -> dict[str, Any]:
    return agent_body("a1", factory_ia_model="contrato", base_url=base_url)
