"""F3-03, ``live``: o índice do RAG semântico com o filtro ``user_id`` num Atlas Vector Search de verdade.

Só rodam com ``MONGO_LIVE_URL`` (marker ``live``; nunca na CI padrão). Banco efêmero por teste
(``tests/fakes/live_mongo.py``); embedder ``OllamaEmbedder`` real do agno 2.5.8 com o cliente do SDK
falso (768 posições). O ``$vectorSearch`` usa o pré-filtro do agno 3.1.12 (``_user_scope_filter``,
``vectordb/mongodb/mongodb.py:522-529``: o dono ou o balde compartilhado ``user_id: null``): prova que o
índice criado e o reparado aceitam o filtro e que os chunks gravados pelo 2.5.8 (com ``user_id: null``,
e os legados marcados assim pelo reparo) contam como compartilhados. ``MONGO_LIVE_URL`` não entra em
``assert``, id de teste nem log nosso.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from agno.knowledge.embedder.base import Embedder
from agno.knowledge.embedder.ollama import OllamaEmbedder
from pymongo.errors import OperationFailure
from pymongo.operations import SearchIndexModel

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.model_config import ModelConfig
from src.domain.entities.rag_config import RagConfig
from src.domain.ports.embedder_factory_port import IEmbedderFactory
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService, rag_collection_name
from src.infrastructure.runtime.agno.embedder_dimensions import DimensionResolvingEmbedderFactory
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from tests.fakes import FakeModelFactory, InMemoryToolRepository, RecordingLogger
from tests.fakes.embedders import FakeOllamaClient, unit_vector
from tests.fakes.live_mongo import LiveMongo

pytestmark = pytest.mark.live

AGENT_ID = "suporte"
COLLECTION = rag_collection_name(AGENT_ID)
DOC = "# Manual\n\nIntro do manual.\n\n## Faturas\n\nA segunda via da fatura sai no portal.\n"
DIMENSIONS = 768
_QUERYABLE_TIMEOUT_S = 90.0
USER_FILTER = {"type": "filter", "path": "user_id"}


class _Ollama(IEmbedderFactory):
    def __init__(self) -> None:
        self.client = FakeOllamaClient(native_size=DIMENSIONS)

    def create_embedder(self, config: ModelConfig) -> Embedder:
        return OllamaEmbedder(id=config.model_id, ollama_client=self.client)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "manual.md").write_text(DOC, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")


async def _start_agent(live: LiveMongo, logger: RecordingLogger) -> Any:
    service = AgentFactoryService(
        db_url=live.url,
        db_name=live.database,
        session_db=None,
        logger=logger,
        model_factory=FakeModelFactory(),
        embedder_factory=DimensionResolvingEmbedderFactory(_Ollama(), logger=RecordingLogger()),
        tool_factory=HttpToolFactory(logger=logger),
        tool_repository=InMemoryToolRepository(),
    )
    config = AgentConfig(
        id=AGENT_ID,
        nome="Suporte",
        factory_ia_model="ollama",
        model="llama3.2:latest",
        descricao="d",
        prompt="p",
        rag_config=RagConfig(
            active=True, doc_name="manual.md", factory_ia_model="ollama", model="nomic-embed-text:latest"
        ),
    )
    return await service.create_agent(config)


def _index(live: LiveMongo) -> dict[str, Any]:
    [index] = list(live.collection(COLLECTION).list_search_indexes())
    return dict(index)


def _fields(index: dict[str, Any]) -> list[dict[str, Any]]:
    definition = index.get("latestDefinition") or index["definition"]
    return list(definition["fields"])


def _seed_owned_chunks(live: LiveMongo) -> None:
    """Chunks como o agno 3.x grava (campo ``user_id``): um de cada dono e um compartilhado."""
    live.collection(COLLECTION).insert_many(
        [
            {"_id": f"chunk-{owner}", "content": f"texto de {owner}", "content_hash": "h3",
             "embedding": unit_vector(f"texto de {owner}", DIMENSIONS), "user_id": user_id}
            for owner, user_id in (("u1", "u1"), ("u2", "u2"), ("compartilhado", None))
        ]
    )  # fmt: skip


def _search_as(live: LiveMongo, user_id: str, expected: int) -> set[str]:
    """``$vectorSearch`` com o pré-filtro do agno 3.x até achar ``expected`` chunks (indexação assíncrona)."""
    pipeline = [
        {
            "$vectorSearch": {
                "index": "vector_index_1",
                "path": "embedding",
                "queryVector": unit_vector("fatura", DIMENSIONS),
                "numCandidates": 100,
                "limit": 20,
                "filter": {"$or": [{"user_id": user_id}, {"user_id": None}]},
            }
        },
        {"$project": {"_id": 1}},
    ]
    deadline = time.monotonic() + _QUERYABLE_TIMEOUT_S
    while True:
        try:
            found = {str(doc["_id"]) for doc in live.collection(COLLECTION).aggregate(pipeline)}
        except OperationFailure:  # índice ainda não consultável no Atlas: tenta de novo até o prazo
            if time.monotonic() > deadline:
                raise
            found = set()
        if len(found) >= expected or time.monotonic() > deadline:
            return found
        time.sleep(1)


def _agent_chunk_ids(live: LiveMongo) -> set[str]:
    return {str(doc["_id"]) for doc in live.collection(COLLECTION).find({"content_hash": {"$ne": "h3"}}, {"_id": 1})}


async def test_live_indice_novo_declara_user_id_e_o_vector_search_filtra_por_dono(live_mongo: LiveMongo) -> None:
    agent = await _start_agent(live_mongo, RecordingLogger())
    assert agent.knowledge is not None
    assert USER_FILTER in _fields(_index(live_mongo))
    _seed_owned_chunks(live_mongo)
    from_agent = _agent_chunk_ids(live_mongo)  # gravados pelo 2.5.8, com user_id: null
    assert from_agent

    found = _search_as(live_mongo, "u1", expected=len(from_agent) + 2)

    assert found == from_agent | {"chunk-u1", "chunk-compartilhado"}  # nunca o chunk-u2


async def test_live_indice_legado_sem_filtro_e_recriado_e_o_vector_search_filtra(live_mongo: LiveMongo) -> None:
    """Índice da dimensão certa mas sem ``user_id`` (o do F2-11 / agno 2.5.8): o startup recria só o índice."""
    collection = live_mongo.collection(COLLECTION)
    collection.insert_one(
        {"_id": "legado", "content": "A segunda via da fatura sai no portal.", "content_hash": "h0",
         "embedding": unit_vector("legado", DIMENSIONS)}
    )  # fmt: skip
    vector_only = {
        "fields": [{"type": "vector", "numDimensions": DIMENSIONS, "path": "embedding", "similarity": "cosine"}]
    }
    collection.create_search_index(SearchIndexModel(definition=vector_only, name="vector_index_1", type="vectorSearch"))
    legacy_index = _index(live_mongo)
    assert USER_FILTER not in _fields(legacy_index)
    logger = RecordingLogger()

    agent = await _start_agent(live_mongo, logger)

    assert agent.knowledge is not None
    repaired = _index(live_mongo)
    assert repaired["id"] != legacy_index["id"] and USER_FILTER in _fields(repaired)
    assert [r.context.get("previous_dimensions") for r in logger.records if r.level == "warning"] == [DIMENSIONS]
    assert collection.find_one({"_id": "legado"}, {"embedding": 1})["embedding"] == unit_vector("legado", DIMENSIONS)
    _seed_owned_chunks(live_mongo)
    expected = _agent_chunk_ids(live_mongo) | {"chunk-u2", "chunk-compartilhado"}

    assert _search_as(live_mongo, "u2", expected=len(expected)) == expected  # o legado incluso, nunca o chunk-u1

    again_logger = RecordingLogger()
    again = await _start_agent(live_mongo, again_logger)
    assert again.knowledge is not None
    assert _index(live_mongo)["id"] == repaired["id"]  # 2º startup não recria
    assert not [r for r in again_logger.records if r.level in {"warning", "error"}]
