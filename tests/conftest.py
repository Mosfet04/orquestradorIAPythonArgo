"""Configurações compartilhadas para todos os testes."""

from __future__ import annotations

import os
from collections.abc import Iterator
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from tests.fakes.knowledge import offline_knowledge  # noqa: F401  (fixture compartilhada)

TESTS_DIR = Path(__file__).resolve().parent

# Diretório de primeiro nível em tests/ -> marker da pirâmide aplicado a todo teste dele.
_LAYER_BY_DIR = {
    "unit": "unit",
    "golden": "unit",
    "contract": "contract",
    "integration": "integration",
    "security": "security",
    "eval": "eval",
}


def pytest_addoption(parser: pytest.Parser) -> None:
    # Fica aqui (raiz de tests/) porque pytest só registra opções de conftest carregado
    # na inicialização; em tests/golden/conftest.py a opção não existiria em `pytest tests/unit`.
    parser.addoption(
        "--update-golden",
        action="store_true",
        default=False,
        help="Regrava os snapshots de tests/golden/snapshots em vez de comparar.",
    )


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    """Marca cada teste pela camada do diretório em que está (ver ``_LAYER_BY_DIR``)."""
    for item in items:
        try:
            top = item.path.resolve().relative_to(TESTS_DIR).parts[0]
        except ValueError:
            continue  # teste fora de tests/ (ex.: pytester)
        layer = _LAYER_BY_DIR.get(top)
        if layer is None:
            raise pytest.UsageError(
                f"{item.nodeid}: diretório tests/{top} sem camada; mova o teste ou mapeie em _LAYER_BY_DIR"
            )
        item.add_marker(layer)


@pytest.fixture(scope="session", autouse=True)
def setup_test_environment():
    """Configura variáveis de ambiente para testes."""
    test_env = {
        "MONGO_CONNECTION_STRING": os.getenv(
            "MONGO_CONNECTION_STRING", "mongodb://localhost:27017"
        ),
        "MONGO_DATABASE_NAME": os.getenv("MONGO_DATABASE_NAME", "testdb"),
        "APP_TITLE": "Orquestrador de Agentes IA",
        "LOG_LEVEL": "ERROR",
    }
    with patch.dict(os.environ, test_env):
        # Auth da borda (F1-04): sem chaves nem APP_HOST herdados do shell, o app dos
        # testes sobe no modo dev local; quem testa auth define as chaves no próprio teste.
        for name in ("API_KEY_RUN", "API_KEY_ADMIN", "APP_HOST"):
            os.environ.pop(name, None)
        yield


@pytest.fixture(autouse=True)
def isolated_agno_run_cancellation() -> Iterator[None]:
    """Gerenciador de cancelamento de run do agno novo a cada teste.

    O agno 2.5.8 guarda cancelamentos num gerenciador global do processo e um cancel de
    run inexistente fica como intenção pendente. Sem isto, ``POST /agents/x/runs/r1/cancel``
    num teste cancela o próximo run ``r1`` de outro teste no mesmo processo (ordem
    aleatória, workers do xdist). Usa a API pública ``get_/set_cancellation_manager`` e
    devolve o gerenciador original no teardown.
    """
    from agno.run import cancel
    from agno.run.cancellation_management.in_memory_cancellation_manager import InMemoryRunCancellationManager

    previous = cancel.get_cancellation_manager()
    cancel.set_cancellation_manager(InMemoryRunCancellationManager())
    yield
    cancel.set_cancellation_manager(previous)


@pytest.fixture
def reset_otel_providers(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """Isola os providers globais do OpenTelemetry para testes que os configuram.

    A API do OTel só aceita ``set_*_provider`` uma vez por processo e liga os proxies
    globais ao provider real. Aqui cada teste começa com globais e proxies novos; no fim
    os providers criados são desligados (o que também remove o handler de atexit) e o
    monkeypatch devolve os globais originais. Mesmo procedimento de
    ``opentelemetry.test.globals_test`` (pacote opentelemetry-test-utils, não instalado).
    """
    from opentelemetry import trace
    from opentelemetry.metrics import _internal as metrics_api
    from opentelemetry.util._once import Once

    monkeypatch.setattr(trace, "_TRACER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(trace, "_TRACER_PROVIDER", None)
    monkeypatch.setattr(trace, "_PROXY_TRACER_PROVIDER", trace.ProxyTracerProvider())
    monkeypatch.setattr(metrics_api, "_METER_PROVIDER_SET_ONCE", Once())
    monkeypatch.setattr(metrics_api, "_METER_PROVIDER", None)
    monkeypatch.setattr(metrics_api, "_PROXY_METER_PROVIDER", metrics_api._ProxyMeterProvider())
    yield
    for provider in (trace._TRACER_PROVIDER, metrics_api._METER_PROVIDER):
        shutdown = getattr(provider, "shutdown", None)
        if callable(shutdown):
            shutdown()


@pytest.fixture
def mock_logger():
    """Mock para ILogger."""
    logger = MagicMock()
    logger.info = MagicMock()
    logger.warning = MagicMock()
    logger.error = MagicMock()
    logger.debug = MagicMock()
    return logger


@pytest.fixture
def mock_agent_config_repository():
    """Mock assíncrono para IAgentConfigRepository."""
    repo = AsyncMock()
    repo.get_active_agents = AsyncMock(return_value=[])
    repo.get_agent_by_id = AsyncMock(return_value=None)
    return repo


@pytest.fixture
def mock_tool_repository():
    """Mock assíncrono para IToolRepository."""
    repo = AsyncMock()
    repo.get_tools_by_ids = AsyncMock(return_value=[])
    repo.get_all_active_tools = AsyncMock(return_value=[])
    repo.get_tool_by_id = AsyncMock(return_value=None)
    return repo


@pytest.fixture
def mock_team_config_repository():
    """Mock assíncrono para ITeamConfigRepository."""
    repo = AsyncMock()
    repo.get_active_teams = AsyncMock(return_value=[])
    repo.get_team_by_id = AsyncMock(return_value=None)
    return repo
