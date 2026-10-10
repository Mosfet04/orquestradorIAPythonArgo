"""F2-03: plugins de provider no startup real (``AppFactory`` -> ``DependencyContainer``).

Critérios: duplicata (plugin x built-in) recusa o startup com mensagem clara, antes do Mongo e
do bind (o lifespan do uvicorn roda antes de abrir o socket); o composition root liga
``PLUGIN_ALLOWLIST`` ao loader; ``DYNAMIC_PROVIDER_SPECS`` só importa com
``ALLOW_DYNAMIC_IMPORT=true`` no modo dev local e, fora disso, recusa o startup sem importar.
"""

from __future__ import annotations

import pytest
from starlette.testclient import TestClient

from src.infrastructure import dependency_injection as di
from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.dependency_injection import DependencyContainer
from src.infrastructure.providers.plugins import PluginLoadError
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes.plugins import FakeSite, spec_module

_ENV = (
    "PLUGIN_ALLOWLIST", "ALLOW_DYNAMIC_IMPORT", "DYNAMIC_PROVIDER_SPECS", "ENVIRONMENT", "APP_HOST",
    "API_KEY_RUN", "API_KEY_ADMIN", "MODEL_BASE_URL_ALLOWLIST", "SECRETS_DIR",
)
KEYS = {"API_KEY_RUN": "r" * 32, "API_KEY_ADMIN": "a" * 32}


class MongoReached(Exception):
    """O startup passou dos providers e chegou ao Mongo."""


@pytest.fixture
def mongo_calls(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    calls: list[str] = []

    def _client(connection_string: str, **_: object) -> None:
        calls.append("mongo")
        raise MongoReached

    for name in _ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(di, "AsyncIOMotorClient", _client)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    monkeypatch.setenv("OTEL_ENABLED", "false")
    return calls


def test_plugin_conflitando_com_built_in_derruba_o_startup_antes_do_mongo(
    plugin_site: FakeSite, mongo_calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    plugin_site.install("acme-plugin", {"acme": "acme_boot_dup_mod:SPEC"}, {"acme_boot_dup_mod": spec_module("openai")})
    monkeypatch.setenv("PLUGIN_ALLOWLIST", "acme-plugin:acme")
    app = AppFactory().create_app()

    with pytest.raises(PluginLoadError) as caught:
        with TestClient(app):
            pass  # pragma: no cover - o startup não pode completar

    assert str(caught.value) == (
        "plugin 'acme-plugin:acme': provider 'openai' com id ou alias já registrado (openai: built-in); "
        "id e aliases de provider são únicos, sem diferenciar caixa"
    )
    assert mongo_calls == []


async def test_plugin_da_allowlist_e_carregado_pelo_composition_root(
    plugin_site: FakeSite, mongo_calls: list[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    module = plugin_site.side_effect_module("acme_boot_ok_mod")
    plugin_site.install("acme-plugin", {"acme": "acme_boot_ok_mod:SPEC"}, {"acme_boot_ok_mod": module})
    monkeypatch.setenv("PLUGIN_ALLOWLIST", "Acme_Plugin:acme")

    with pytest.raises(MongoReached):
        await DependencyContainer.create_async(AppConfig.load())

    assert plugin_site.imported("acme_boot_ok_mod")
    assert mongo_calls == ["mongo"]


@pytest.mark.parametrize(
    ("env", "allowed"),
    [
        ({"ALLOW_DYNAMIC_IMPORT": "true"}, True),  # modo dev local: sem chaves, loopback, development
        ({"ALLOW_DYNAMIC_IMPORT": "true", "ENVIRONMENT": "test"}, True),
        ({}, False),  # sem o flag, nem no modo dev local
        ({"ALLOW_DYNAMIC_IMPORT": "false"}, False),
        ({"ALLOW_DYNAMIC_IMPORT": "true", **KEYS}, False),  # com chaves não é modo dev local
        ({"ALLOW_DYNAMIC_IMPORT": "true", "APP_HOST": "0.0.0.0"}, False),  # noqa: S104 - bind fora do loopback
        ({"ALLOW_DYNAMIC_IMPORT": "true", "ENVIRONMENT": "staging", **KEYS}, False),
    ],
)
async def test_import_dinamico_so_no_modo_dev_local_com_o_flag(
    plugin_site: FakeSite, mongo_calls: list[str], monkeypatch: pytest.MonkeyPatch, env: dict[str, str], allowed: bool
) -> None:
    plugin_site.install("dyn-holder", {}, {"dyn_boot_mod": plugin_site.side_effect_module("dyn_boot_mod", "dyn")})
    monkeypatch.setenv("DYNAMIC_PROVIDER_SPECS", "dyn_boot_mod:SPEC")
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    config = AppConfig.load()

    if allowed:
        with pytest.raises(MongoReached):
            await DependencyContainer.create_async(config)
        assert plugin_site.imported("dyn_boot_mod")
        return
    with pytest.raises(PluginLoadError) as caught:
        await DependencyContainer.create_async(config)
    assert not plugin_site.imported("dyn_boot_mod")
    assert mongo_calls == []
    assert "dyn_boot_mod" not in str(caught.value)
    assert "ALLOW_DYNAMIC_IMPORT=true" in str(caught.value) and "modo dev local" in str(caught.value)


async def test_flag_sem_import_dinamico_nao_muda_nada(mongo_calls: list[str], monkeypatch: pytest.MonkeyPatch) -> None:
    """``ALLOW_DYNAMIC_IMPORT=true`` sozinho (sem ``DYNAMIC_PROVIDER_SPECS``) não importa nada nem recusa."""
    monkeypatch.setenv("ALLOW_DYNAMIC_IMPORT", "true")
    monkeypatch.setenv("ENVIRONMENT", "production")
    for name, value in KEYS.items():
        monkeypatch.setenv(name, value)

    with pytest.raises(MongoReached):
        await DependencyContainer.create_async(AppConfig.load())
