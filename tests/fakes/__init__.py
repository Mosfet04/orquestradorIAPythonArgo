"""Fakes de teste: no lugar de LLM, embedder, logger e repositórios reais (sem rede, sem Mongo)."""

from tests.fakes.logger import LoggedRecord, RecordingLogger
from tests.fakes.models import (
    FakeChatModel,
    FakeEmbedder,
    FakeEmbedderFactory,
    FakeModelCall,
    FakeModelFactory,
    ScriptExhaustedError,
    running_on_event_loop,
)
from tests.fakes.repositories import (
    InMemoryAgentConfigRepository,
    InMemoryDocumentTreeRepository,
    InMemoryTeamConfigRepository,
    InMemoryToolRepository,
)
from tests.fakes.telemetry import RecordingTelemetryMetrics
from tests.fakes.web import loopback_client

__all__ = [
    "FakeChatModel",
    "FakeEmbedder",
    "FakeEmbedderFactory",
    "FakeModelCall",
    "FakeModelFactory",
    "InMemoryAgentConfigRepository",
    "InMemoryDocumentTreeRepository",
    "InMemoryTeamConfigRepository",
    "InMemoryToolRepository",
    "LoggedRecord",
    "RecordingLogger",
    "RecordingTelemetryMetrics",
    "ScriptExhaustedError",
    "loopback_client",
    "running_on_event_loop",
]
