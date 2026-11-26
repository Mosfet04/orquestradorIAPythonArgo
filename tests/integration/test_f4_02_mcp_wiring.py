"""F4-02: o composition root liga as sessões MCP à guarda das tools e as fecha no cleanup; sem o extra, o app sobe.

Container de produção sem Mongo (o ping falha e o app sobe) e sem DNS real (``system_resolver`` falso).
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path
from typing import ClassVar

import pytest

from src.infrastructure import dependency_injection as di
from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.mcp_client.sessions import McpSessions
from src.infrastructure.repositories import mongo_base
from src.infrastructure.security.guarded_http import GuardedAsyncTransport
from src.infrastructure.security.ssrf import DestinationRule
from tests.fakes.admin_config_app import admin_env
from tests.fakes.dns import public_dns
from tests.fakes.mongo import trap_sync_pymongo

ROOT = Path(__file__).resolve().parents[2]


class _RecordingSessions(McpSessions):
    built: ClassVar[list[_RecordingSessions]] = []

    def __init__(self, *args: object, **kwargs: object) -> None:
        super().__init__(*args, **kwargs)  # type: ignore[arg-type]
        self.kwargs = kwargs
        self.closed = 0
        _RecordingSessions.built.append(self)

    async def aclose(self) -> None:
        self.closed += 1
        await super().aclose()


class _RecordingTransport(GuardedAsyncTransport):
    built: ClassVar[list[tuple[DestinationRule, object]]] = []

    def __init__(self, rule: DestinationRule, **kwargs: object) -> None:
        _RecordingTransport.built.append((rule, kwargs.get("resolver")))
        super().__init__(rule, **kwargs)  # type: ignore[arg-type]


@pytest.fixture
def env(monkeypatch: pytest.MonkeyPatch) -> None:
    admin_env(monkeypatch)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    monkeypatch.setenv("MONGO_CONNECTION_STRING", "mongodb://mongo.test.invalid:27017")
    monkeypatch.setenv("TOOL_ROUTE_ALLOWLIST", "mcp.interno")
    monkeypatch.setenv("SECRETS_DIR", "/run/segredos")
    monkeypatch.setattr(mongo_base.MongoClientFactory, "_instances", {})
    monkeypatch.setattr(di, "system_resolver", public_dns)
    monkeypatch.setattr(di, "McpSessions", _RecordingSessions)
    monkeypatch.setattr(di, "GuardedAsyncTransport", _RecordingTransport)
    _RecordingSessions.built = []
    _RecordingTransport.built = []
    trap_sync_pymongo(monkeypatch)


async def test_sessoes_mcp_usam_a_regra_das_tools_e_o_cleanup_as_fecha(env: None):
    container = await di.DependencyContainer.create_async(AppConfig.load())
    [sessions] = _RecordingSessions.built
    transport_factory = sessions.kwargs["transport_factory"]
    transport = transport_factory()  # type: ignore[operator]
    try:
        rule = DestinationRule(trusted_hosts=frozenset({"mcp.interno"}), allow_loopback=False)
        # o das tools HTTP no startup e o da sessão MCP, com a mesma regra e o mesmo resolver
        assert _RecordingTransport.built == [(rule, public_dns), (rule, public_dns)]
        assert isinstance(transport, GuardedAsyncTransport)
        assert sessions.kwargs["secrets_dir"] == "/run/segredos"
    finally:
        await transport.aclose()
        await container.cleanup()
        await container.cleanup()

    assert sessions.closed == 2  # idempotente: a 2ª não faz nada


def test_sem_o_extra_mcp_os_modulos_do_app_importam():
    code = (
        "import sys; sys.modules['mcp'] = None\n"
        "import src.infrastructure.dependency_injection\n"
        "import src.infrastructure.web.app_factory\n"
        "import src.infrastructure.runtime.agno.mcp_tool_factory\n"
        "print('ok')\n"
    )
    done = subprocess.run(  # noqa: S603 - o próprio interpretador, código fixo do teste
        [sys.executable, "-c", code], cwd=ROOT, capture_output=True, text=True, timeout=120, check=False
    )

    assert (done.returncode, done.stdout.strip()) == (0, "ok"), done.stderr[-2000:]
