"""F4-01: o composition root monta o transporte guardado das tools com a regra da config e fecha os clientes HTTP.

Container de produção sem Mongo (o ping falha e o app sobe) e sem DNS real (``system_resolver`` falso).
"""

from __future__ import annotations

import pytest

from src.infrastructure import dependency_injection as di
from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.providers import ProviderRegistry
from src.infrastructure.repositories import mongo_base
from src.infrastructure.security.guarded_http import GuardedAsyncTransport
from src.infrastructure.security.ssrf import DestinationRule
from tests.fakes.admin_config_app import admin_env
from tests.fakes.dns import public_dns
from tests.fakes.mongo import trap_sync_pymongo


class _Built:
    def __init__(self) -> None:
        self.rules: list[DestinationRule] = []
        self.resolvers: list[object] = []
        self.closed = 0


@pytest.fixture
def built(monkeypatch: pytest.MonkeyPatch) -> _Built:
    admin_env(monkeypatch)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    monkeypatch.setenv("MONGO_CONNECTION_STRING", "mongodb://mongo.test.invalid:27017")
    monkeypatch.setattr(mongo_base.MongoClientFactory, "_instances", {})
    monkeypatch.setattr(di, "system_resolver", public_dns)
    trap_sync_pymongo(monkeypatch)
    state = _Built()

    class Recording(GuardedAsyncTransport):
        def __init__(self, rule: DestinationRule, **kwargs: object) -> None:
            state.rules.append(rule)
            state.resolvers.append(kwargs.get("resolver"))
            super().__init__(rule, **kwargs)  # type: ignore[arg-type]

        async def aclose(self) -> None:
            state.closed += 1
            await super().aclose()

    monkeypatch.setattr(di, "GuardedAsyncTransport", Recording)
    return state


async def test_tools_usam_a_allowlist_e_o_resolver_do_app_e_o_cleanup_fecha(
    monkeypatch: pytest.MonkeyPatch, built: _Built
):
    monkeypatch.setenv("TOOL_ROUTE_ALLOWLIST", "API.Interna.example., 10.0.0.5")
    closed_registries: list[ProviderRegistry] = []
    real_aclose = ProviderRegistry.aclose

    async def spy(self: ProviderRegistry) -> None:
        closed_registries.append(self)
        await real_aclose(self)

    monkeypatch.setattr(ProviderRegistry, "aclose", spy)
    container = await di.DependencyContainer.create_async(AppConfig.load())
    await container.cleanup()
    await container.cleanup()

    assert built.rules == [
        DestinationRule(trusted_hosts=frozenset({"api.interna.example", "10.0.0.5"}), allow_loopback=False)
    ]
    assert built.resolvers == [public_dns]
    assert built.closed == 1  # nenhuma chamada saiu: o transporte injetado é fechado direto, uma vez
    assert len(closed_registries) == 2  # idempotente: o segundo não tem o que fechar


async def test_modo_dev_local_libera_o_loopback_na_conexao(monkeypatch: pytest.MonkeyPatch, built: _Built):
    monkeypatch.setenv("ENVIRONMENT", "development")
    monkeypatch.setenv("APP_HOST", "127.0.0.1")
    for name in ("API_KEY_RUN", "API_KEY_ADMIN"):
        monkeypatch.delenv(name)
    container = await di.DependencyContainer.create_async(AppConfig.load())
    await container.cleanup()

    assert built.rules == [DestinationRule(trusted_hosts=frozenset(), allow_loopback=True)]
