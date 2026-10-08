"""Telemetria do Agno desligada (F1-03) em Agent, Team e AgentOS por padrão.

Agent/Team: ``tests/golden/test_agno_kwargs_golden.py`` prova ``telemetry=False`` em toda
montagem. Aqui: o AgentOS e o default de ``AGNO_TELEMETRY`` no startup.

Resíduo conhecido (Agno 2.5.8, revisão na F3): evals criados pela rota ``/eval-runs`` do
AgentOS (``agno/os/routers/evals/utils.py``) usam ``telemetry=True`` e ignoram
``AGNO_TELEMETRY``.
"""

from __future__ import annotations

import os
from typing import Any, ClassVar

import agno.api.os as agno_api_os
import pytest
from agno.agent import Agent
from agno.os import AgentOS

from src.infrastructure.web import app_factory
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import FakeChatModel


class _RecordingAgentOS(AgentOS):
    created: ClassVar[list[AgentOS]] = []

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        _RecordingAgentOS.created.append(self)


def test_agent_os_montado_sem_telemetria(monkeypatch: pytest.MonkeyPatch):
    launches: list[object] = []
    monkeypatch.setattr(agno_api_os, "log_os_telemetry", lambda launch: launches.append(launch))
    monkeypatch.setattr(app_factory, "AgentOS", _RecordingAgentOS)
    monkeypatch.setattr(_RecordingAgentOS, "created", [])
    agent = Agent(id="agente-1", name="Agente 1", model=FakeChatModel(responses=[]), telemetry=False)
    factory = AppFactory()

    factory._mount_agent_os(factory.create_app(), [agent], [])

    assert [os_.telemetry for os_ in _RecordingAgentOS.created] == [False]
    assert launches == [], "AgentOS registrou o launch na API da Agno"


def _reset_agno_telemetry(monkeypatch: pytest.MonkeyPatch) -> None:
    # setenv antes do delenv: o monkeypatch passa a restaurar o valor original no teardown.
    monkeypatch.setenv("AGNO_TELEMETRY", "placeholder")
    monkeypatch.delenv("AGNO_TELEMETRY")


def test_startup_define_agno_telemetry_false_quando_ausente(monkeypatch: pytest.MonkeyPatch):
    _reset_agno_telemetry(monkeypatch)

    AppFactory().create_app()

    assert os.environ["AGNO_TELEMETRY"] == "false"


def test_startup_nao_sobrescreve_valor_explicito(monkeypatch: pytest.MonkeyPatch):
    _reset_agno_telemetry(monkeypatch)
    monkeypatch.setenv("AGNO_TELEMETRY", "true")

    AppFactory().create_app()

    assert os.environ["AGNO_TELEMETRY"] == "true"
