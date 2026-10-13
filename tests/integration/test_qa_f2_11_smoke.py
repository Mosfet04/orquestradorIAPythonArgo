"""QA do F2-11: smoke do app real com RAG (AppFactory -> DependencyContainer -> AgentFactoryService).

Pilha de produção inteira (config em YAML, container com o ``DimensionResolvingEmbedderFactory``,
``Knowledge``/``GuardedMongoVectorDb`` do agno, ``IndexingService`` hierárquico, AgentOS) em processo
(``TestClient``, sem porta, sem rede). Só as bordas são falsas: o Mongo (de config e de árvore,
``startup_world``), o driver síncrono do vector db (``tests/fakes/vector_mongo.py``, com uma busca
``$vectorSearch`` por cosseno), o cliente do SDK do Ollama (``FakeOllamaClient``, 768 posições) e o
modelo de chat (roteirizado: chama a tool de busca e responde com o que ela devolveu).

Cenários do item: (1) agente RAG semântico/hierárquico do ``rag_config`` legado (sem ``dimensions``)
sobe, é listado e o run usa o conhecimento; (2) coleção legada gravada com ``embedding: []`` é corrigida
no startup; (3) embedder fora do ar: o app sobe, o agente sem RAG, log com ``error_type`` e sem texto
do documento; nada de I/O síncrono no event loop.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import agno.vectordb.mongodb.mongodb as agno_vector_module
import pytest
from agno.knowledge import Knowledge
from agno.knowledge.embedder.ollama import OllamaEmbedder
from agno.models.response import ModelResponse
from agno.vectordb.mongodb import MongoDb as MongoVectorDb
from starlette.testclient import TestClient

from src.infrastructure import dependency_injection as di
from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.runtime.agno.agent_factory_service import rag_collection_name
from src.infrastructure.web import app_factory
from tests.fakes import FakeChatModel, FakeModelCall, RecordingLogger, running_on_event_loop
from tests.fakes.embedders import FakeOllamaClient, unit_vector
from tests.fakes.startup_world import CLEAN_ENV, Events, startup_world
from tests.fakes.vector_mongo import FakeVectorCollection, FakeVectorServer, install_fake_vector_mongo
from tests.fakes.web import LOOPBACK_BASE_URL, LOOPBACK_CLIENT

_KEYWORDS = ("fatura", "reembolso", "segunda", "via")
RUN_KEY = "qa-f211-run-key-" + "r" * 20
ADMIN_KEY = "qa-f211-admin-key-" + "a" * 18
RUN = {"Authorization": f"Bearer {RUN_KEY}"}
MARK_SEM = "MARCA-SEMANTICA-SEGUNDA-VIA"
MARK_HIER = "MARCA-HIERARQUICA-REEMBOLSO"
DOC_SEM = f"# Faturas\n\n## Segunda via\n\nPara a segunda via da fatura use o portal. {MARK_SEM}.\n"
DOC_HIER = (
    f"# Financeiro\n\nIntrodução.\n\n## Reembolso\n\n{MARK_HIER} envie a nota fiscal.\n\n## Fatura\n\nVence dia 5.\n"
)

CONFIG = """
agents:
  - id: semantico
    nome: Semantico
    factoryIaModel: ollama
    model: m-chat
    descricao: d
    prompt: [Responda com base no conhecimento.]
    rag_config: {active: true, doc_name: faturas.md}
    active: true
  - id: hierarquico
    nome: Hierarquico
    factoryIaModel: ollama
    model: m-chat
    descricao: d
    prompt: [Responda com base no conhecimento.]
    rag_config: {active: true, doc_name: financeiro.md, search_strategy: hierarchical}
    active: true
  - id: comum
    nome: Sem RAG
    factoryIaModel: ollama
    model: m-chat
    descricao: d
    prompt: [Sem conhecimento.]
    active: true
"""


@dataclass
class _ThreadAwareOllama(FakeOllamaClient):
    """``FakeOllamaClient`` que anota se a chamada de rede rodou no event loop."""

    on_loop: list[bool] = field(default_factory=list)

    def embed(self, model: str = "", input: str = "", **kwargs: Any) -> Any:  # pylint: disable=redefined-builtin
        self.on_loop.append(running_on_event_loop())
        result = super().embed(model=model, input=input, **kwargs)
        if self.error is not None or not self.native_size:
            return result
        # Um eixo por palavra-chave (o resto zero): relevância previsível, no tamanho nativo do modelo.
        counts = [float(str(input).lower().count(word)) for word in _KEYWORDS]
        vector = [*counts, 0.0 if any(counts) else 1.0]
        norm = math.sqrt(sum(x * x for x in vector))
        padded = [x / norm for x in vector] + [0.0] * (self.native_size - len(vector))
        return {"embeddings": [padded]}


class _SearchBrain(FakeChatModel):
    """Chama a tool de busca que o agente oferece e responde com o que ela devolveu."""

    def _next_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        call: FakeModelCall = self._as_call(args, kwargs)
        self.calls.append(call)
        tool_results = [content for role, content in call.messages if role == "tool"]
        if tool_results:
            return ModelResponse(role="assistant", content="LI: " + " | ".join(tool_results))
        for name in ("search_knowledge_base", "search_knowledge"):
            if name in call.tool_names:
                arguments = json.dumps({"query": "fatura reembolso"})
                return ModelResponse(
                    role="assistant",
                    tool_calls=[{"id": "c1", "type": "function", "function": {"name": name, "arguments": arguments}}],
                )
        return ModelResponse(role="assistant", content="SEM-BUSCA")


def _aggregate(collection: FakeVectorCollection, pipeline: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """``$vectorSearch`` (cosseno) + ``$set score`` + ``$project embedding: 0``: o que o agno 2.5.8 manda."""
    collection._use("aggregate")
    stage = pipeline[0]["$vectorSearch"]
    query = stage["queryVector"]

    def cosine(vector: list[float]) -> float:
        if len(vector) != len(query):
            return -1.0
        norm = math.sqrt(sum(x * x for x in vector)) * math.sqrt(sum(x * x for x in query))
        return sum(a * b for a, b in zip(vector, query, strict=True)) / (norm or 1.0)

    ranked = sorted(
        (d for d in collection.docs.values() if d.get("embedding")), key=lambda d: cosine(d["embedding"]), reverse=True
    )
    return [
        {**{k: v for k, v in d.items() if k != "embedding"}, "score": cosine(d["embedding"])}
        for d in ranked[: stage["limit"]]
    ]


class _AsyncCursor:
    def __init__(self, docs: list[dict[str, Any]]) -> None:
        self._docs = docs

    def __aiter__(self) -> Iterator[Any]:
        return self._iterate()

    async def _iterate(self) -> Any:
        for doc in self._docs:
            yield doc


class _AsyncCollection:
    """Busca assíncrona (``AsyncMongoClient``) sobre a mesma coleção em memória da síncrona."""

    def __init__(self, collection: FakeVectorCollection) -> None:
        self._collection = collection

    async def aggregate(self, pipeline: list[dict[str, Any]]) -> _AsyncCursor:
        assert running_on_event_loop()  # a busca assíncrona roda no loop, como o driver async
        return _AsyncCursor(_aggregate(self._collection, pipeline))


class _AsyncDatabase:
    def __init__(self, server: FakeVectorServer, name: str) -> None:
        self._server, self._name = server, name

    def __getitem__(self, collection: str) -> _AsyncCollection:
        return _AsyncCollection(self._server.collection(self._name, collection))


class _AsyncAdmin:
    async def command(self, _name: str) -> dict[str, int]:
        return {"ok": 1}


class _AsyncMongoClient:
    def __init__(self, server: FakeVectorServer) -> None:
        self._server = server
        self.admin = _AsyncAdmin()

    def __getitem__(self, database: str) -> _AsyncDatabase:
        return _AsyncDatabase(self._server, database)


class _AsyncOllama:
    """``ollama.AsyncClient.embed`` sobre o mesmo cliente falso (mesmo tamanho nativo, mesmas chamadas)."""

    def __init__(self, client: _ThreadAwareOllama) -> None:
        self._client = client

    async def embed(self, **kwargs: Any) -> Any:
        return self._client.embed(**kwargs)


@dataclass
class World:
    http: TestClient
    logger: RecordingLogger
    vectors: FakeVectorServer
    ollama: _ThreadAwareOllama
    db_name: str
    legacy_ids: set[Any] = field(default_factory=set)


def _write_legacy(vectors: FakeVectorServer, db_name: str, db_url: str) -> set[Any]:
    """O que o código de antes do F2-11 gravava para o ``rag_config`` legado: ``[]`` e índice de 4096."""
    knowledge = Knowledge(
        vector_db=MongoVectorDb(
            collection_name=rag_collection_name("semantico"),
            db_url=db_url,
            database=db_name,
            embedder=OllamaEmbedder(id="nomic-embed-text:latest", ollama_client=FakeOllamaClient()),  # type: ignore[arg-type]
        )
    )
    knowledge.insert(path="docs/faturas.md", skip_if_exists=True)
    legacy = vectors.collection(db_name, rag_collection_name("semantico"))
    assert legacy.docs and {len(d["embedding"]) for d in legacy.docs.values()} == {0}  # o defeito, antes do app
    assert _index_dimensions(legacy) == [4096]
    return set(legacy.docs)


@contextmanager
def smoke_app(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    ollama: _ThreadAwareOllama | None = None,
    legacy: bool = False,
) -> Iterator[World]:
    for name in CLEAN_ENV:
        monkeypatch.delenv(name, raising=False)
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "faturas.md").write_text(DOC_SEM, encoding="utf-8")
    (docs / "financeiro.md").write_text(DOC_HIER, encoding="utf-8")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    for name, value in {
        "AGNO_TELEMETRY": "false",
        "OTEL_ENABLED": "false",
        "CONFIG_STORE": "yaml",
        "CONFIG_YAML_PATH": str(config_path),
        "ENVIRONMENT": "production",
        "APP_HOST": "127.0.0.1",
        "API_KEY_RUN": RUN_KEY,
        "API_KEY_ADMIN": ADMIN_KEY,
    }.items():
        monkeypatch.setenv(name, value)
    app_config = AppConfig.load()
    vectors = install_fake_vector_mongo(monkeypatch)
    monkeypatch.setattr(FakeVectorCollection, "aggregate", _aggregate, raising=False)
    monkeypatch.setattr(agno_vector_module, "AsyncMongoClient", lambda *_, **__: _AsyncMongoClient(vectors))
    legacy_ids: set[Any] = set()
    if legacy:
        legacy_ids = _write_legacy(vectors, app_config.mongo_database_name, app_config.mongo_connection_string)
    client = ollama or _ThreadAwareOllama(native_size=768)
    logger = RecordingLogger()

    def create_embedder(self: Any, config: Any) -> OllamaEmbedder:
        # Como o registry: OllamaEmbedder do agno sem ``dimensions`` quando o documento não traz.
        kwargs = {"dimensions": config.params["dimensions"]} if "dimensions" in config.params else {}
        return OllamaEmbedder(  # type: ignore[arg-type]
            id=config.model_id, ollama_client=client, async_client=_AsyncOllama(client), **kwargs
        )

    def create_model(self: Any, config: Any) -> FakeChatModel:
        return _SearchBrain(id=config.model_id, responses=["resumo do modelo"] * 50)

    with (
        startup_world(Events(), agents=()),
        patch.object(di.ProviderRegistry, "create_embedder", create_embedder),
        patch.object(di.ProviderRegistry, "create_model", create_model),
        patch.object(di, "StructlogLoggerAdapter", lambda _name: logger),
    ):
        app = app_factory.AppFactory().create_app()
        with TestClient(app, base_url=LOOPBACK_BASE_URL, client=LOOPBACK_CLIENT, raise_server_exceptions=False) as http:
            yield World(http, logger, vectors, client, app_config.mongo_database_name, legacy_ids)


def _run(world: World, agent_id: str) -> str:
    response = world.http.post(f"/agents/{agent_id}/runs", data={"message": "oi", "stream": "false"}, headers=RUN)
    assert response.status_code == 200, response.text
    return str(response.json()["content"])


def _collection(world: World) -> FakeVectorCollection:
    return world.vectors.collection(world.db_name, rag_collection_name("semantico"))


def _index_dimensions(collection: FakeVectorCollection) -> list[int]:
    return [i["latestDefinition"]["fields"][0]["numDimensions"] for i in collection.search_indexes]


def test_startup_com_rag_legado_do_ollama_lista_os_agentes_e_o_run_usa_o_conhecimento(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with smoke_app(monkeypatch, tmp_path) as world:
        startup_on_loop = list(world.ollama.on_loop)  # sonda + indexação do startup
        listed = world.http.get("/agents", headers=RUN)
        semantic = _run(world, "semantico")
        hierarchical = _run(world, "hierarquico")
        plain = _run(world, "comum")
        collection = _collection(world)

    assert sorted(a["id"] for a in listed.json()) == ["comum", "hierarquico", "semantico"]
    # semântico: coleção e índice com 768 e a busca devolveu o trecho ao modelo
    assert {len(d["embedding"]) for d in collection.docs.values()} == {768}
    assert _index_dimensions(collection) == [768]
    assert MARK_SEM in semantic
    # hierárquico: os nós da árvore têm 768 e a busca top-down devolveu o trecho
    assert MARK_HIER in hierarchical
    assert plain == "SEM-BUSCA"
    # uma sonda por config (o mesmo ollama + nomic para os dois RAGs) e nenhuma rede no event loop
    probes = [c for c in world.ollama.calls if c["input"] == "dimensão"]
    assert len(probes) == 1
    assert startup_on_loop and not any(startup_on_loop)
    assert not [op for op, on_loop in collection.ops if on_loop and op != "aggregate"]  # aggregate: driver async
    assert not [r for r in world.logger.records if r.level == "error"]
    assert MARK_SEM not in repr(world.logger.records) and MARK_HIER not in repr(world.logger.records)


def test_startup_corrige_a_colecao_legada_com_embedding_vazio_e_a_busca_acha_o_trecho(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    with smoke_app(monkeypatch, tmp_path, legacy=True) as world:
        collection = _collection(world)
        reply = _run(world, "semantico")

    assert MARK_SEM in reply  # a busca do app já sobe sobre a coleção corrigida
    assert {len(d["embedding"]) for d in collection.docs.values()} == {768}
    assert set(collection.docs) == world.legacy_ids  # reindexado no lugar: nenhum chunk novo nem apagado
    assert _index_dimensions(collection) == [768]
    assert collection.dropped_search_indexes == ["vector_index_1"]
    warnings = [r.context for r in world.logger.records if r.level == "warning" and r.context.get("agent_id")]
    assert any(c.get("reembedded") == len(world.legacy_ids) for c in warnings)
    assert any(c.get("previous_dimensions") == 4096 and c.get("dimensions") == 768 for c in warnings)
    assert not [r for r in world.logger.records if r.level == "error"]
    assert MARK_SEM not in repr(world.logger.records)


def test_embedder_fora_do_ar_o_app_sobe_e_os_agentes_ficam_sem_rag_com_log_so_do_tipo(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    sdk_text = "texto-da-excecao-do-sdk"
    down = _ThreadAwareOllama(error=ConnectionError(sdk_text))

    with smoke_app(monkeypatch, tmp_path, ollama=down) as world:
        listed = world.http.get("/agents", headers=RUN)
        semantic = _run(world, "semantico")
        hierarchical = _run(world, "hierarquico")
        health = world.http.get("/admin/health", headers={"Authorization": f"Bearer {ADMIN_KEY}"})

    assert health.status_code == 200
    assert sorted(a["id"] for a in listed.json()) == ["comum", "hierarquico", "semantico"]
    assert semantic == "SEM-BUSCA" and hierarchical == "SEM-BUSCA"  # nenhuma tool de busca oferecida
    assert not world.vectors.created  # nenhuma coleção criada
    error_types = [r.context["error_type"] for r in world.logger.records if "error_type" in r.context]
    assert "ConnectionError" in error_types and "EmbedderProbeError" in error_types
    assert sdk_text not in repr(world.logger.records)
    assert MARK_SEM not in repr(world.logger.records) and MARK_HIER not in repr(world.logger.records)
    assert not any(world.ollama.on_loop)


def test_a_sonda_so_de_uma_vez_e_a_busca_usa_o_mesmo_tamanho(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A consulta é embutida com o mesmo embedder (768): o vetor da pergunta casa com o do índice."""
    with smoke_app(monkeypatch, tmp_path) as world:
        _run(world, "semantico")

    queries = [c for c in world.ollama.calls if c["input"] == "fatura reembolso"]
    assert queries and "dimensions" in queries[0]  # depois da sonda o agno pede o tamanho nativo
    assert queries[0]["dimensions"] == 768
    assert len(unit_vector("fatura reembolso", 768)) == 768
