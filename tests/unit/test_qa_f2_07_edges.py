"""QA do F2-07: bordas que os testes do dev não cobrem.

- controller sem callbacks de métrica (o padrão): hit e miss não registram nada e nada quebra;
- ``build_provider_registry(config, logger)`` usa o logger recebido (não um global) e o ``AppConfig``
  recebido (não o ambiente do processo no momento da chamada);
- a tool HTTP movida para ``runtime/agno`` não devolve ao modelo o texto da exceção (BUG-F2-07-QA-1,
  corrigido no próprio F2-07: o resultado leva só o tipo).
"""

from __future__ import annotations

import httpx
import pytest
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase
from src.application.use_cases.get_active_teams_use_case import GetActiveTeamsUseCase
from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.tool import HttpMethod, ParameterType, Tool, ToolParameter
from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.dependency_injection import build_provider_registry
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from src.infrastructure.telemetry import metrics as metrics_module
from src.presentation.controllers.orquestrador_controller import OrquestradorController
from tests.fakes import InMemoryAgentConfigRepository, InMemoryTeamConfigRepository, RecordingLogger
from tests.fakes.plugins import FakeSite, spec_module
from tests.fakes.runtime import FakeAgentRuntime
from tests.fakes.startup_world import CLEAN_ENV

MARKER = "mongodb://app:s3nh4-do-driver@db.interno.invalid:27017/?authSource=admin"


def _controller(**callbacks: object) -> OrquestradorController:
    logger = RecordingLogger()
    runtime = FakeAgentRuntime()
    agents = InMemoryAgentConfigRepository(
        [AgentConfig(id="a1", nome="A", factory_ia_model="ollama", model="m", descricao="d", prompt="p")]
    )
    return OrquestradorController(
        get_active_agents_use_case=GetActiveAgentsUseCase(runtime, agents, logger),
        get_active_teams_use_case=GetActiveTeamsUseCase(runtime, InMemoryTeamConfigRepository(), logger),
        logger=logger,
        **callbacks,  # type: ignore[arg-type]
    )


async def test_controller_sem_callbacks_serve_hit_e_miss_sem_registrar_metrica(monkeypatch: pytest.MonkeyPatch) -> None:
    reader = InMemoryMetricReader()
    provider = MeterProvider(metric_readers=[reader])
    meter = provider.get_meter("qa-f2-07")
    monkeypatch.setattr(metrics_module, "cache_hits_total", meter.create_counter("cache_hits_total"))
    monkeypatch.setattr(metrics_module, "cache_misses_total", meter.create_counter("cache_misses_total"))
    controller = _controller()

    first = await controller.get_agents()  # miss
    second = await controller.get_agents()  # hit
    teams = await controller.get_teams()  # hit de agents + carga de teams

    assert [a.id for a in first] == ["a1"] and second is first
    assert teams == []
    data = reader.get_metrics_data()
    provider.shutdown()
    recorded = [m.name for r in (data.resource_metrics if data else []) for s in r.scope_metrics for m in s.metrics]
    assert recorded == []  # nenhum ponto: o controller só repassa a quem foi ligado no composition root


async def test_callbacks_do_controller_recebem_so_o_nome_do_cache_na_ordem_dos_eventos() -> None:
    events: list[tuple[str, str]] = []
    controller = _controller(
        on_cache_hit=lambda name: events.append(("hit", name)),
        on_cache_miss=lambda name: events.append(("miss", name)),
    )

    await controller.get_teams()  # miss de teams; os agentes que ele pede também são miss
    await controller.get_teams()
    await controller.get_agents()

    assert events == [("miss", "teams"), ("miss", "agents"), ("hit", "teams"), ("hit", "agents")]


def _env_limpo(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (*CLEAN_ENV, "OLLAMA_BASE_URL"):
        monkeypatch.delenv(name, raising=False)


def test_build_provider_registry_loga_no_logger_recebido_e_segue_a_config_recebida_nao_o_ambiente(
    plugin_site: FakeSite, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_limpo(monkeypatch)
    module = "qa_f2_07_acme_mod"
    plugin_site.install("acme-plugin", {"acme": f"{module}:SPEC"}, {module: spec_module("acme")})
    config = AppConfig.load()  # sem PLUGIN_ALLOWLIST: o plugin instalado é ignorado
    monkeypatch.setenv("PLUGIN_ALLOWLIST", "acme-plugin:acme")  # depois do load: a função não pode relê-lo
    logger = RecordingLogger()

    registry = build_provider_registry(config, logger)

    assert "acme" not in registry.supported("chat")
    [ignored] = [r for r in logger.records if r.message == "Plugin de provider ignorado: fora da PLUGIN_ALLOWLIST"]
    assert ignored.level == "warning" and ignored.context["distribution"] == "acme-plugin"


def test_build_provider_registry_com_plugin_da_allowlist_registra_o_provider_e_loga_no_logger_recebido(
    plugin_site: FakeSite, monkeypatch: pytest.MonkeyPatch
) -> None:
    _env_limpo(monkeypatch)
    module = "qa_f2_07_acme_ok_mod"
    plugin_site.install("acme-plugin", {"acme": f"{module}:SPEC"}, {module: spec_module("acme")})
    monkeypatch.setenv("PLUGIN_ALLOWLIST", "acme-plugin:acme")
    logger = RecordingLogger()

    registry = build_provider_registry(AppConfig.load(), logger)

    assert "acme" in registry.supported("chat")
    assert "Plugin de provider carregado" in logger.messages("info")


_TOOL = Tool(
    id="busca",
    name="Busca",
    description="Busca pedidos",
    route="https://api.example.invalid/pedidos",
    http_method=HttpMethod.GET,
    parameters=[ToolParameter(name="q", type=ParameterType.STRING, description="termo")],
)


async def test_resultado_da_tool_http_nao_ecoa_o_texto_da_excecao(monkeypatch: pytest.MonkeyPatch) -> None:
    real_client = httpx.AsyncClient

    def handler(request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"falha interna: {MARKER}")

    monkeypatch.setattr(
        httpx, "AsyncClient", lambda **kwargs: real_client(transport=httpx.MockTransport(handler), **kwargs)
    )
    (function,) = await HttpToolFactory(logger=RecordingLogger()).create_tools_from_configs([_TOOL])

    result = await function.entrypoint(q="x")

    assert result == "Erro inesperado ao chamar a tool (RuntimeError)"
