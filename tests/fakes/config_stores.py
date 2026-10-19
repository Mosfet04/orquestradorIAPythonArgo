"""Backends de config (``CONFIG_STORE``) para a suíte de contrato: memória, Mongo (coleção fake), YAML
e, só com ``MONGO_LIVE_URL`` (marker ``live``), Mongo real (``mongo-live``).

Cada backend monta os repositórios das portas ``IAgentConfigRepository``, ``ITeamConfigRepository``
e ``IToolRepository`` a partir de **documentos** (o formato gravado no Mongo e escrito no YAML) ou de
**entidades** (convertidas para documento por ``agent_document``/``team_document``/``tool_document``).
A ordem da lista é a ordem estável do backend: ``_id`` crescente no Mongo, ordem no arquivo no YAML.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Awaitable, Callable, Iterable, Mapping
from enum import Enum
from pathlib import Path
from typing import Any

import pytest
import yaml
from bson import ObjectId

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.team_config import TeamConfig
from src.domain.entities.tool import HttpMethod, Tool
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
from tests.fakes.live_mongo import LiveMongo
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


class MongoLiveBackend:
    """Repositórios Mongo reais sobre um Mongo de verdade (banco efêmero do ``live_mongo``).

    Confere a fidelidade da coleção fake (F2-06, R5): a mesma suíte de contrato roda aqui. ``name``
    é ``"mongo"`` porque o contrato é o do backend Mongo (ex.: o log traz ``mongo_id``). Os
    documentos são gravados com o pymongo síncrono, ``_id`` crescente na ordem da lista, e lidos
    pelos repositórios com um cliente motor do próprio teste.
    """

    name = "mongo"

    def __init__(self, monkeypatch: pytest.MonkeyPatch, live: LiveMongo) -> None:
        self._live = live
        client = live.motor()
        monkeypatch.setattr(mongo_base.MongoClientFactory, "get_client", classmethod(lambda cls, _conn: client))

    def _collection(self, name: str, docs: Iterable[Document]) -> None:
        collection = self._live.collection(name)
        collection.delete_many({})
        documents = [{"_id": ObjectId(), **doc} for doc in docs]
        if documents:
            collection.insert_many(documents)

    def agents(self, docs: Iterable[Document], logger: RecordingLogger | None = None) -> IAgentConfigRepository:
        self._collection("agents_config", docs)
        return MongoAgentConfigRepository(
            connection_string=_CONN, database_name=self._live.database, logger=logger or RecordingLogger()
        )

    def teams(self, docs: Iterable[Document], logger: RecordingLogger | None = None) -> ITeamConfigRepository:
        self._collection("teams_config", docs)
        return MongoTeamConfigRepository(
            connection_string=_CONN, database_name=self._live.database, logger=logger or RecordingLogger()
        )

    def tools(self, docs: Iterable[Document], logger: RecordingLogger | None = None) -> IToolRepository:
        self._collection("tools", docs)
        return MongoToolRepository(
            connection_string=_CONN, database_name=self._live.database, logger=logger or RecordingLogger()
        )


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

    def __init__(self, backend: MongoBackend | MongoLiveBackend | YamlBackend) -> None:
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
LIVE_BACKEND = "mongo-live"
_LIVE_PARAM = pytest.param(LIVE_BACKEND, marks=pytest.mark.live)
DOCUMENT_BACKEND_PARAMS = (*DOCUMENT_BACKENDS, _LIVE_PARAM)
"""``params`` da fixture de backend de documentos: os de sempre e o Mongo real (marker ``live``)."""
ENTITY_BACKEND_PARAMS = (*ENTITY_BACKENDS, _LIVE_PARAM)


def live_mongo_for(request: pytest.FixtureRequest) -> LiveMongo | None:
    """O ``live_mongo`` do teste se o parâmetro da fixture for ``mongo-live`` (pula sem a URL)."""
    if request.param != LIVE_BACKEND:
        return None
    live: LiveMongo = request.getfixturevalue("live_mongo")
    return live


def document_backend(
    name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, live: LiveMongo | None = None
) -> MongoBackend | MongoLiveBackend | YamlBackend:
    if name == "mongo":
        return MongoBackend(monkeypatch)
    if name == "yaml":
        return YamlBackend(tmp_path)
    if name == LIVE_BACKEND and live is not None:
        return MongoLiveBackend(monkeypatch, live)
    raise ValueError(f"backend de documentos desconhecido: {name}")


def entity_backend(
    name: str, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, live: LiveMongo | None = None
) -> MemoryBackend | EntityBackend:
    if name == "memory":
        return MemoryBackend()
    return EntityBackend(document_backend(name, monkeypatch, tmp_path, live))


WRITABLE_BACKEND_PARAMS = ("memory", "mongo", _LIVE_PARAM)
"""Backends por entidades que aceitam escrita (FA-01; o YAML é só leitura). Mongo sem índice
único de ``id``: a escrita não pode depender dele."""


# ── os três tipos de config, para a suíte de contrato da escrita (FA-01) ─────────


def make_agent(entity_id: str, label: str = "v0", *, active: bool = True) -> AgentConfig:
    return AgentConfig(
        id=entity_id, nome=label, factory_ia_model="ollama", model="m", descricao="d", prompt="p", active=active
    )


def make_team(entity_id: str, label: str = "v0", *, active: bool = True) -> TeamConfig:
    return TeamConfig(id=entity_id, nome=label, factory_ia_model="ollama", model="m", member_ids=["a"], active=active)


def make_tool(entity_id: str, label: str = "v0", *, active: bool = True) -> Tool:
    return Tool(
        id=entity_id,
        name=label,
        description="d",
        route="https://example.invalid",
        http_method=HttpMethod.GET,
        parameters=[],
        active=active,
    )


@dataclasses.dataclass(frozen=True)
class ConfigKind:
    """Uma das três portas: como montar a entidade, o repositório e as leituras do runtime."""

    make: Callable[..., Any]
    repo: Callable[[Any, list[Any]], Any]
    get: Callable[[Any, str], Awaitable[Any]]
    active: Callable[[Any], Awaitable[list[Any]]]

    @staticmethod
    def label(entity: Any) -> str:
        """O rótulo que ``make`` grava: ``nome`` em agent/team, ``name`` na tool."""
        return str(entity.name if isinstance(entity, Tool) else entity.nome)


CONFIG_KINDS = {
    "agent": ConfigKind(
        make_agent, lambda b, i: b.agent_repo(i), lambda r, i: r.get_agent_by_id(i), lambda r: r.get_active_agents()
    ),
    "team": ConfigKind(
        make_team, lambda b, i: b.team_repo(i), lambda r, i: r.get_team_by_id(i), lambda r: r.get_active_teams()
    ),
    "tool": ConfigKind(
        make_tool, lambda b, i: b.tool_repo(i), lambda r, i: r.get_tool_by_id(i), lambda r: r.get_all_active_tools()
    ),
}


# ── documentos de exemplo da suíte de contrato de documentos ─────────

MARKER = "SEGREDO-QUE-NAO-PODE-IR-AO-LOG"
"""Texto plantado nos documentos de exemplo: nenhum log pode trazê-lo."""
NOT_A_LIST = "Lista de ids que não é lista ignorada"
NOT_TEXT_ITEMS = "Itens que não são texto ignorados na lista de ids"


def marked_agent_doc(agent_id: Any = "ok", **overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "id": agent_id,
        "nome": "Agente",
        "factoryIaModel": "ollama",
        "model": "m",
        "descricao": "d",
        "prompt": [MARKER],
        "tools_ids": [],
        "rag_config": {"active": False},
        "active": True,
    }
    doc.update(overrides)
    return doc


def marked_team_doc(team_id: Any = "time", **overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "id": team_id,
        "nome": "Time",
        "factoryIaModel": "ollama",
        "model": "m",
        "prompt": MARKER,
        "member_ids": ["ok"],
        "mode": "route",
        "active": True,
    }
    doc.update(overrides)
    return doc


def marked_tool_doc(tool_id: Any = "t", **overrides: Any) -> dict[str, Any]:
    doc: dict[str, Any] = {
        "id": tool_id,
        "name": "Tool",
        "description": MARKER,
        "route": "https://api.example/x",
        "http_method": "GET",
        "parameters": [],
        "active": True,
    }
    doc.update(overrides)
    return doc


def logged_errors(logger: RecordingLogger) -> list[tuple[str, dict[str, Any]]]:
    return [(r.message, r.context) for r in logger.records if r.level == "error"]
