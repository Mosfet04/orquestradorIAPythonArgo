"""QA do F4-01: o ``cleanup`` do container fecha cada cliente HTTP mesmo se o outro falhar (sem citar o texto do erro).

Mesmo container de produção sem Mongo e sem DNS real de ``test_f4_01_http_wiring.py``.
"""

from __future__ import annotations

import pytest

from src.infrastructure import dependency_injection as di
from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.providers import ProviderRegistry
from src.infrastructure.repositories import mongo_base
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from tests.fakes import RecordingLogger
from tests.fakes.admin_config_app import admin_env
from tests.fakes.dns import public_dns
from tests.fakes.mongo import trap_sync_pymongo


@pytest.fixture(autouse=True)
def container_env(monkeypatch: pytest.MonkeyPatch) -> None:
    admin_env(monkeypatch)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    monkeypatch.setenv("MONGO_CONNECTION_STRING", "mongodb://mongo.test.invalid:27017")
    monkeypatch.setattr(mongo_base.MongoClientFactory, "_instances", {})
    monkeypatch.setattr(di, "system_resolver", public_dns)
    trap_sync_pymongo(monkeypatch)


@pytest.mark.parametrize("failing", ["tools", "modelos"])
async def test_falha_ao_fechar_um_cliente_http_nao_impede_o_fechamento_do_outro(
    monkeypatch: pytest.MonkeyPatch, failing: str
):
    closed: list[str] = []

    async def tools_aclose(self: HttpToolFactory) -> None:
        closed.append("tools")
        if failing == "tools":
            raise RuntimeError("texto interno: Authorization: Bearer sk-nao-pode-sair")

    async def models_aclose(self: ProviderRegistry) -> None:
        closed.append("modelos")
        if failing == "modelos":
            raise RuntimeError("texto interno: Authorization: Bearer sk-nao-pode-sair")

    monkeypatch.setattr(HttpToolFactory, "aclose", tools_aclose)
    monkeypatch.setattr(ProviderRegistry, "aclose", models_aclose)
    container = await di.DependencyContainer.create_async(AppConfig.load())
    logger = RecordingLogger()
    monkeypatch.setattr(container, "_logger", logger)

    await container.cleanup()

    assert closed == ["tools", "modelos"]  # os dois foram tentados, na ordem
    failures = [r for r in logger.records if r.message == "Falha ao fechar o cliente HTTP"]
    assert [(r.level, r.context) for r in failures] == [
        ("warning", {"client": failing, "error_type": "RuntimeError"})
    ]
    assert "sk-nao-pode-sair" not in repr(logger.records)
