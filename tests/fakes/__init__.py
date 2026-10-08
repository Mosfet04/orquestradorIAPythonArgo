"""Fakes de teste: no lugar de LLM, embedder, logger e repositórios reais (sem rede, sem Mongo)."""

from tests.fakes.logger import LoggedRecord, RecordingLogger
from tests.fakes.models import (
    FakeChatModel,
    FakeEmbedder,
    FakeEmbedderFactory,
    FakeModelCall,
    FakeModelFactory,
    ScriptExhaustedError,
)
from tests.fakes.repositories import (
    InMemoryAgentConfigRepository,
    InMemoryDocumentTreeRepository,
    InMemoryTeamConfigRepository,
    InMemoryToolRepository,
)

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
    "ScriptExhaustedError",
]
