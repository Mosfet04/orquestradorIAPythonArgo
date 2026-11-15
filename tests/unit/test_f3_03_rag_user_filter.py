"""F3-03: o RAG semântico pronto para o agno 2.5.8 (instalado) e o 3.1.12.

No 3.1.12 (``agno/vectordb/mongodb/mongodb.py``) o ``MongoDb`` chama ``prepare_doc(..., user_id=)``
(``:623``/``:1097``), o índice ``vectorSearch`` declara o filtro ``user_id`` (``:333-338``) e o
``search`` filtra por ele dentro do ``$vectorSearch`` (``:733``); o embedder levanta ``EmbeddingError``
em vez de devolver ``[]``. O 3.x não está instalado: o comportamento dele é simulado na borda (a
assinatura do ``prepare_doc`` do pai, a exceção do embedder), como no F3-02.

Montagem de produção (``AgentFactoryService``, ``Knowledge``/``MongoDb`` reais do agno 2.5.8); só o
driver síncrono do vector db é o servidor em memória de ``tests/fakes/vector_mongo.py``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from agno.knowledge.document import Document
from agno.knowledge.embedder.base import Embedder
from agno.vectordb.mongodb import MongoDb as MongoVectorDb

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.model_config import ModelConfig
from src.domain.entities.rag_config import RagConfig
from src.domain.ports.embedder_factory_port import IEmbedderFactory
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService, rag_collection_name
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from src.infrastructure.runtime.agno.rag_vector_db import EmbeddingDimensionError, GuardedMongoVectorDb
from tests.fakes import FakeEmbedderFactory, FakeModelFactory, InMemoryToolRepository, RecordingLogger
from tests.fakes.embedders import ScriptedEmbedder
from tests.fakes.session_db import fake_session_db
from tests.fakes.vector_mongo import FakeVectorCollection, FakeVectorServer

DB_URL = "mongodb://mongo.test.invalid:27017"
DB_NAME = "test_db"
AGENT_ID = "suporte"
COLLECTION = rag_collection_name(AGENT_ID)
USER_FILTER = {"type": "filter", "path": "user_id"}
LEAK_MARKER = "valor-de-teste-do-provider"  # marcador de vazamento, não é segredo


class EmbeddingError(Exception):
    """No lugar do ``agno.exceptions.EmbeddingError`` do 3.x (o texto do provider pode ter segredo)."""


class _Factory(IEmbedderFactory):
    def __init__(self, embedder: Embedder) -> None:
        self._embedder = embedder

    def create_embedder(self, config: ModelConfig) -> Embedder:
        return self._embedder


def _service(logger: RecordingLogger, embedders: IEmbedderFactory) -> AgentFactoryService:
    return AgentFactoryService(
        db_url=DB_URL,
        db_name=DB_NAME,
        session_db=fake_session_db(),
        logger=logger,
        model_factory=FakeModelFactory(),
        embedder_factory=embedders,
        tool_factory=HttpToolFactory(logger=logger),
        tool_repository=InMemoryToolRepository(),
    )


def _config(*, dimensions: int | None = None) -> AgentConfig:
    return AgentConfig(
        id=AGENT_ID,
        nome="Suporte",
        factory_ia_model="ollama",
        model="llama3.2:latest",
        descricao="d",
        prompt="p",
        rag_config=RagConfig(
            active=True,
            doc_name="manual.md",
            factory_ia_model="ollama",
            model="nomic-embed-text:latest",
            model_params=None if dimensions is None else {"dimensions": dimensions},
        ),
    )


@pytest.fixture(autouse=True)
def docs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "manual.md").write_text("# Manual\n\n## Faturas\n\nA segunda via sai no portal.\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    return docs


def _fields(collection: FakeVectorCollection) -> list[list[dict[str, Any]]]:
    return [index["latestDefinition"]["fields"] for index in collection.search_indexes]


def _vector_db(dimensions: int = 8) -> GuardedMongoVectorDb:
    return GuardedMongoVectorDb(
        collection_name=COLLECTION,
        db_url=DB_URL,
        database=DB_NAME,
        embedder=ScriptedEmbedder(dimensions=dimensions),
        dimensions=dimensions,
    )


# ── índice com o filtro user_id ──────────────────────────────────────


async def test_colecao_nova_nasce_com_o_indice_que_declara_o_filtro_user_id(
    fake_vector_mongo: FakeVectorServer,
) -> None:
    agent = await _service(RecordingLogger(), FakeEmbedderFactory(dimensions=8)).create_agent(_config())

    collection = fake_vector_mongo.collection(DB_NAME, COLLECTION)
    assert agent.knowledge is not None
    assert _fields(collection) == [
        [
            {"type": "vector", "numDimensions": 8, "path": "embedding", "similarity": "cosine"},
            USER_FILTER,
        ]
    ]


async def test_indice_legado_sem_o_filtro_user_id_e_recriado_sem_tocar_nos_chunks(
    fake_vector_mongo: FakeVectorServer,
) -> None:
    chunk = {"_id": "c1", "name": "manual.md", "content": "texto", "content_hash": "h", "embedding": [0.5] * 8}
    collection = fake_vector_mongo.seed(DB_NAME, COLLECTION, [chunk], index_dimensions=8, index_user_filter=False)
    logger = RecordingLogger()

    agent = await _service(logger, FakeEmbedderFactory(dimensions=8)).create_agent(_config())

    assert agent.knowledge is not None
    assert collection.dropped_search_indexes == ["vector_index_1"]
    [fields] = _fields(collection)
    assert USER_FILTER in fields and fields[0]["numDimensions"] == 8
    assert collection.docs["c1"] == {**chunk, "user_id": None}  # embedding intacto; só marcado como compartilhado
    [warning] = [r for r in logger.records if r.level == "warning"]
    assert warning.context == {
        "agent_id": AGENT_ID,
        "collection": COLLECTION,
        "previous_dimensions": 8,
        "dimensions": 8,
    }
    assert not [r for r in logger.records if r.level == "error"]


async def test_reparo_do_indice_sem_filtro_e_idempotente(fake_vector_mongo: FakeVectorServer) -> None:
    collection = fake_vector_mongo.seed(DB_NAME, COLLECTION, [], index_dimensions=8, index_user_filter=False)
    await _service(RecordingLogger(), FakeEmbedderFactory(dimensions=8)).create_agent(_config())
    collection.ops.clear()
    logger = RecordingLogger()

    agent = await _service(logger, FakeEmbedderFactory(dimensions=8)).create_agent(_config())

    assert agent.knowledge is not None
    assert collection.dropped_search_indexes == ["vector_index_1"]  # só o do 1º startup
    assert not [op for op, _ in collection.ops if op in {"create_search_index", "drop_search_index"}]
    assert not [r for r in logger.records if r.level in {"warning", "error"}]


# ── prepare_doc com o user_id do 3.x ──────────────────────────────────


def _prepare_doc_3x(
    self: MongoVectorDb,
    content_hash: str,
    document: Document,
    filters: dict[str, Any] | None = None,
    user_id: str | None = None,
) -> dict[str, Any]:
    """A assinatura e o campo ``user_id`` do ``prepare_doc`` do agno 3.1.12 (``mongodb.py:1097-1127``)."""
    return {
        "_id": f"{content_hash}-{user_id}",
        "content": document.content,
        "embedding": document.embedding,
        "content_hash": content_hash,
        "user_id": user_id,
    }


def test_prepare_doc_repassa_o_user_id_do_3x_ao_agno(
    fake_vector_mongo: FakeVectorServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(MongoVectorDb, "prepare_doc", _prepare_doc_3x)
    vector_db = _vector_db()

    prepared = vector_db.prepare_doc("h", Document(content="texto", embedding=[0.5] * 8), None, user_id="u1")

    assert prepared == {
        "_id": "h-u1",
        "content": "texto",
        "embedding": [0.5] * 8,
        "content_hash": "h",
        "user_id": "u1",
    }
    assert vector_db.rejected_chunks == 0


def test_prepare_doc_com_user_id_recusa_o_embedding_invalido_e_conta(
    fake_vector_mongo: FakeVectorServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """O 3.x pula o documento só com ``ValueError`` (``mongodb.py:577-579``): nunca ``TypeError``."""
    monkeypatch.setattr(MongoVectorDb, "prepare_doc", _prepare_doc_3x)
    vector_db = _vector_db()

    with pytest.raises(EmbeddingDimensionError):
        vector_db.prepare_doc("h", Document(content="texto", embedding=[]), None, user_id="u1")

    assert vector_db.rejected_chunks == 1


# ── erro do embedder no reparo: só o tipo no log ─────────────────────


async def test_embedder_que_levanta_no_reparo_conta_como_falha_e_loga_so_o_tipo(
    fake_vector_mongo: FakeVectorServer,
) -> None:
    """No 3.x o embedder levanta ``EmbeddingError`` (com o texto do provider) em vez de devolver ``[]``."""

    def fail(_text: str) -> int:
        raise EmbeddingError(f"401 Unauthorized: chave {LEAK_MARKER} recusada")

    legacy = [{"_id": "c1", "name": "manual.md", "content": "texto", "content_hash": "h", "embedding": []}]
    collection = fake_vector_mongo.seed(DB_NAME, COLLECTION, legacy, index_dimensions=8)
    logger = RecordingLogger()
    embedders = _Factory(ScriptedEmbedder(dimensions=8, size_for=fail))

    agent = await _service(logger, embedders).create_agent(_config(dimensions=8))

    assert agent.knowledge is None
    assert collection.docs["c1"]["embedding"] == []  # nada apagado nem sobrescrito
    [record] = [r for r in logger.records if r.level in {"warning", "error"}]
    assert (record.level, record.context) == (
        "error",
        {"agent_id": AGENT_ID, "collection": COLLECTION, "failed": 1, "error_type": "EmbeddingError"},
    )
    assert LEAK_MARKER not in repr(logger.records)


# ── chunks sem user_id viram compartilhados (achado do live) ──────────


async def test_chunk_novo_e_gravado_com_user_id_nulo_no_2_5_8(fake_vector_mongo: FakeVectorServer) -> None:
    """No Atlas o pré-filtro ``{user_id: null}`` do 3.x não casa com o campo ausente (live do F3-03)."""
    agent = await _service(RecordingLogger(), FakeEmbedderFactory(dimensions=8)).create_agent(_config())

    docs = list(fake_vector_mongo.collection(DB_NAME, COLLECTION).docs.values())
    assert agent.knowledge is not None
    assert docs and [doc["user_id"] for doc in docs] == [None] * len(docs)


async def test_chunk_legado_sem_user_id_e_marcado_como_compartilhado_no_reparo(
    fake_vector_mongo: FakeVectorServer,
) -> None:
    legacy = {"_id": "c1", "name": "manual.md", "content": "texto", "content_hash": "h", "embedding": [0.5] * 8}
    owned = {**legacy, "_id": "c2", "user_id": "u1"}
    collection = fake_vector_mongo.seed(DB_NAME, COLLECTION, [legacy, owned], index_dimensions=8)
    logger = RecordingLogger()

    agent = await _service(logger, FakeEmbedderFactory(dimensions=8)).create_agent(_config())

    assert agent.knowledge is not None
    assert collection.docs["c1"] == {**legacy, "user_id": None}
    assert collection.docs["c2"] == owned  # o dono de quem já tem não muda
    [record] = [r for r in logger.records if r.context.get("shared") is not None]
    assert (record.level, record.context) == ("info", {"agent_id": AGENT_ID, "collection": COLLECTION, "shared": 1})
