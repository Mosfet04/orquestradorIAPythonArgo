"""Backends de config (``CONFIG_STORE``) para a suíte de contrato: memória, Mongo (coleção fake) e YAML.

Cada backend monta os repositórios das portas ``IAgentConfigRepository``, ``ITeamConfigRepository``
e ``IToolRepository`` a partir de **documentos** (o formato gravado no Mongo e escrito no YAML) ou de
**entidades** (convertidas para documento por ``agent_document``/``team_document``/``tool_document``).
A ordem da lista é a ordem estável do backend: ``_id`` crescente no Mongo, ordem no arquivo no YAML.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Iterable, Mapping
from enum import Enum
from pathlib import Path
from typing import Any

import pytest
import yaml
from bson import ObjectId

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.team_config import TeamConfig
from src.domain.entities.tool import Tool
from src.domain.repositories.agent_config_repository import IAgentConfigRepository
from src.domain.repositories.team_config_repository import ITeamConfigRepository
from src.domain.repositories.tool_repository import IToolRepository
from src.infrastructure.repositories import mongo_base
from src.infrastructure.repositories.mongo_agent_config_repository import MongoAgentConfigRepository
from src.infrastructure.repositories.mongo_team_config_repository import MongoTeamConfigRepository
from src.infrastructure.repositories.mongo_tool_repository import MongoToolRepository
from src.infrastructure.repositories.yaml_config_repository import (
    YamlAgentConfigRepository,
    YamlConfigFile,
    YamlTeamConfigRepository,
    YamlToolRepository,
)
from tests.fakes.logger import RecordingLogger
from tests.fakes.mongo import FakeMongoClient, FakeMongoCollection
from tests.fakes.repositories import (
    InMemoryAgentConfigRepository,
    InMemoryTeamConfigRepository,
    InMemoryToolRepository,
)

Document = dict[str, Any]
_CONN = "mongodb://mongo.invalid"


def _plain(value: Any) -> Any:
    """Entidade -> estrutura só de dict/list/escalares (enum vira o valor), como no documento."""
    if isinstance(value, Enum):
        return value.value
    if isinstance(value, Mapping):
        return {key: _plain(item) for key, item in value.items()}
    if isinstance(value, list | tuple):
        return [_plain(item) for item in value]
    return value


def agent_document(config: AgentConfig) -> Document:
    """Documento (snake_case) que o mapper lê de volta como ``config``."""
    return _plain(dataclasses.asdict(config))


def team_document(config: TeamConfig) -> Document:
    return _plain(dataclasses.asdict(config))


def tool_document(tool: Tool) -> Document:
    return _plain(dataclasses.asdict(tool))


class MongoBackend:
    """Repositórios Mongo reais sobre ``FakeMongoCollection``; ``_id`` crescente na ordem da lista."""

    name = "mongo"

    def __init__(self, monkeypatch: pytest.MonkeyPatch) -> None:
        self.collections: dict[str, FakeMongoCollection] = {}
        client = FakeMongoClient(self.collections)
        monkeypatch.setattr(mongo_base.MongoClientFactory, "get_client", classmethod(lambda cls, _conn: client))

    def _collection(self, name: str, docs: Iterable[Document]) -> None:
        self.collections[name] = FakeMongoCollection([{"_id": ObjectId(), **doc} for doc in docs])

    def agents(self, docs: Iterable[Document], logger: RecordingLogger | None = None) -> IAgentConfigRepository:
        self._collection("agents_config", docs)
        return MongoAgentConfigRepository(connection_string=_CONN, logger=logger or RecordingLogger())

    def teams(self, docs: Iterable[Document], logger: RecordingLogger | None = None) -> ITeamConfigRepository:
        self._collection("teams_config", docs)
        return MongoTeamConfigRepository(connection_string=_CONN, logger=logger or RecordingLogger())

    def tools(self, docs: Iterable[Document], logger: RecordingLogger | None = None) -> IToolRepository:
        self._collection("tools", docs)
        return MongoToolRepository(connection_string=_CONN, logger=logger or RecordingLogger())


class _NoAliasDumper(yaml.SafeDumper):
    """O mesmo objeto em dois documentos (ex.: uma lista de ids) vira cópia, não âncora/alias (recusados)."""

    def ignore_aliases(self, data: Any) -> bool:
        return True


class YamlBackend:
    """Repositórios YAML reais sobre um arquivo em ``tmp_path`` (as três seções no mesmo arquivo)."""

    name = "yaml"

    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / "config.yaml"
        self.sections: dict[str, list[Any]] = {}

    def _source(self, logger: RecordingLogger) -> YamlConfigFile:
        """Um ``YamlConfigFile`` por repositório (o arquivo é regravado a cada seção), com o mesmo logger."""
        return YamlConfigFile(str(self.path), logger=logger)

    def _write(self, section: str, docs: Iterable[Any]) -> None:
        self.sections[section] = list(docs)
        text = yaml.dump(self.sections, Dumper=_NoAliasDumper, allow_unicode=True, sort_keys=False)
        self.path.write_text(text, encoding="utf-8")

    def agents(self, docs: Iterable[Any], logger: RecordingLogger | None = None) -> IAgentConfigRepository:
        self._write("agents", docs)
        logger = logger or RecordingLogger()
        return YamlAgentConfigRepository(self._source(logger), logger=logger)

    def teams(self, docs: Iterable[Any], logger: RecordingLogger | None = None) -> ITeamConfigRepository:
        self._write("teams", docs)
        logger = logger or RecordingLogger()
        return YamlTeamConfigRepository(self._source(logger), logger=logger)

    def tools(self, docs: Iterable[Any], logger: RecordingLogger | None = None) -> IToolRepository:
        self._write("tools", docs)
        logger = logger or RecordingLogger()
        return YamlToolRepository(self._source(logger), logger=logger)


class MemoryBackend:
    """Fakes em memória de ``tests/fakes/repositories.py`` (recebem entidades, não documentos)."""

    name = "memory"

    def agent_repo(self, configs: Iterable[AgentConfig]) -> IAgentConfigRepository:
        return InMemoryAgentConfigRepository(configs)

    def team_repo(self, configs: Iterable[TeamConfig]) -> ITeamConfigRepository:
        return InMemoryTeamConfigRepository(configs)

    def tool_repo(self, tools: Iterable[Tool]) -> IToolRepository:
        return InMemoryToolRepository(tools)


class EntityBackend:
    """Adapta um backend de documentos para receber entidades (contrato comum aos três backends)."""

    def __init__(self, backend: MongoBackend | YamlBackend) -> None:
        self.name = backend.name
        self._backend = backend

    def agent_repo(self, configs: Iterable[AgentConfig]) -> IAgentConfigRepository:
        return self._backend.agents([agent_document(c) for c in configs])

    def team_repo(self, configs: Iterable[TeamConfig]) -> ITeamConfigRepository:
        return self._backend.teams([team_document(c) for c in configs])

    def tool_repo(self, tools: Iterable[Tool]) -> IToolRepository:
        return self._backend.tools([tool_document(t) for t in tools])


DOCUMENT_BACKENDS = ("mongo", "yaml")
ENTITY_BACKENDS = ("memory", *DOCUMENT_BACKENDS)


def document_backend(name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> MongoBackend | YamlBackend:
    if name == "mongo":
        return MongoBackend(monkeypatch)
    if name == "yaml":
        return YamlBackend(tmp_path)
    raise ValueError(f"backend de documentos desconhecido: {name}")


def entity_backend(
    name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> MemoryBackend | EntityBackend:
    if name == "memory":
        return MemoryBackend()
    return EntityBackend(document_backend(name, monkeypatch, tmp_path))
