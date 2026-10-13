"""F2-11, ``live``: o RAG semântico contra um Mongo com Atlas Vector Search de verdade (Atlas Local, F2-10).

Só rodam com ``MONGO_LIVE_URL`` (marker ``live``; nunca na CI padrão); sem ela são pulados. Banco efêmero
por teste (``tests/fakes/live_mongo.py``). O embedder é o ``OllamaEmbedder`` real do agno 2.5.8 com o
cliente do SDK falso (``FakeOllamaClient``, 768 posições como o ``nomic-embed-text``): nenhum Ollama nem
modelo. Cobrem o que o servidor em memória não prova: o índice ``vectorSearch`` com o ``numDimensions``
certo, a remoção assíncrona do índice antigo no Atlas e a consulta ``$vectorSearch`` depois da correção.
``MONGO_LIVE_URL`` não entra em ``assert``, id de teste nem log nosso.
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Any

import pytest
from agno.knowledge import Knowledge
from agno.knowledge.embedder.base import Embedder
from agno.knowledge.embedder.ollama import OllamaEmbedder
from agno.vectordb.mongodb import MongoDb as MongoVectorDb

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.model_config import ModelConfig
from src.domain.entities.rag_config import RagConfig
from src.domain.ports.embedder_factory_port import IEmbedderFactory
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService, rag_collection_name
from src.infrastructure.runtime.agno.embedder_dimensions import DimensionResolvingEmbedderFactory
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from tests.fakes import FakeModelFactory, InMemoryToolRepository, RecordingLogger
from tests.fakes.embedders import FakeOllamaClient
from tests.fakes.live_mongo import LiveMongo

pytestmark = pytest.mark.live

AGENT_ID = "suporte"
COLLECTION = rag_collection_name(AGENT_ID)
DOC = "# Manual\n\nIntro do manual.\n\n## Faturas\n\nA segunda via da fatura sai no portal.\n"
_QUERYABLE_TIMEOUT_S = 60.0


class _Ollama(IEmbedderFactory):
    """``OllamaEmbedder`` do agno sem ``dimensions`` (o ``rag_config`` legado) e sem rede."""

    def __init__(self) -> None:
        self.client = FakeOllamaClient(native_size=768)

    def create_embedder(self, config: ModelConfig) -> Embedder:
        return OllamaEmbedder(id=config.model_id, ollama_client=self.client)  # type: ignore[arg-type]


@pytest.fixture(autouse=True)
def _workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "manual.md").write_text(DOC, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")


def _service(live: LiveMongo, logger: RecordingLogger, embedders: IEmbedderFactory) -> AgentFactoryService:
    return AgentFactoryService(
        db_url=live.url,
        db_name=live.database,
        session_db=None,
        logger=logger,
        model_factory=FakeModelFactory(),
        embedder_factory=embedders,
        tool_factory=HttpToolFactory(logger=logger),
        tool_repository=InMemoryToolRepository(),
    )


def _config() -> AgentConfig:
    return AgentConfig(
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


def _index(live: LiveMongo) -> dict[str, Any]:
    [index] = list(live.collection(COLLECTION).list_search_indexes())
    return dict(index)


def _num_dimensions(index: dict[str, Any]) -> int:
    definition = index.get("latestDefinition") or index["definition"]
    [field] = [f for f in definition["fields"] if f["type"] == "vector"]
    return int(field["numDimensions"])


def _sizes(live: LiveMongo) -> list[int]:
    return sorted(len(doc["embedding"]) for doc in live.collection(COLLECTION).find({}, {"embedding": 1}))


def _search_until_found(knowledge: Knowledge, query: str) -> list[str]:
    """``$vectorSearch`` pelo agno até o índice ficar consultável (Atlas indexa em segundo plano)."""
    deadline = time.monotonic() + _QUERYABLE_TIMEOUT_S
    while True:
        found = [doc.content for doc in knowledge.search(query)]
        if found or time.monotonic() > deadline:
            return found
        time.sleep(1)


def _write_legacy(live: LiveMongo) -> None:
    """O que o código de antes do F2-11 gravava (agno puro): índice 4096 e ``embedding: []``."""
    knowledge = Knowledge(
        vector_db=MongoVectorDb(
            collection_name=COLLECTION,
            db_url=live.url,
            database=live.database,
            embedder=_Ollama().create_embedder(ModelConfig("ollama", "nomic-embed-text:latest")),
        )
    )
    knowledge.insert(path="docs/manual.md", skip_if_exists=True)


async def test_live_colecao_nova_nasce_com_indice_da_dimensao_real_e_busca(live_mongo: LiveMongo) -> None:
    logger = RecordingLogger()
    embedders = DimensionResolvingEmbedderFactory(_Ollama(), logger=logger)

    agent = await _service(live_mongo, logger, embedders).create_agent(_config())

    assert agent.knowledge is not None
    assert _num_dimensions(_index(live_mongo)) == 768
    assert _sizes(live_mongo) and set(_sizes(live_mongo)) == {768}
    assert any("segunda via" in c for c in _search_until_found(agent.knowledge, "fatura"))  # type: ignore[arg-type]
    assert not [r for r in logger.records if r.level in {"warning", "error"}]


async def test_live_embedding_vazio_nunca_e_gravado(live_mongo: LiveMongo) -> None:
    logger = RecordingLogger()

    agent = await _service(live_mongo, logger, _Ollama()).create_agent(_config())  # sem a sonda: O2

    assert agent.knowledge is None
    assert live_mongo.collection(COLLECTION).count_documents({}) == 0
    assert [r.context["error_type"] for r in logger.records if r.level == "error"] == ["EmbeddingDimensionError"]


async def test_live_colecao_legada_e_corrigida_no_startup_e_a_correcao_e_idempotente(live_mongo: LiveMongo) -> None:
    _write_legacy(live_mongo)
    legacy_ids = {doc["_id"] for doc in live_mongo.collection(COLLECTION).find({}, {"_id": 1})}
    assert _num_dimensions(_index(live_mongo)) == 4096 and set(_sizes(live_mongo)) == {0}  # O1 + O2 no Atlas
    embedders = DimensionResolvingEmbedderFactory(_Ollama(), logger=RecordingLogger())
    logger = RecordingLogger()

    agent = await _service(live_mongo, logger, embedders).create_agent(_config())

    assert agent.knowledge is not None
    first_index = _index(live_mongo)
    assert _num_dimensions(first_index) == 768
    assert {doc["_id"] for doc in live_mongo.collection(COLLECTION).find({}, {"_id": 1})} == legacy_ids
    assert set(_sizes(live_mongo)) == {768}
    assert any("segunda via" in c for c in _search_until_found(agent.knowledge, "fatura"))  # type: ignore[arg-type]
    assert sorted(r.context.get("reembedded", 0) for r in logger.records if r.level == "warning")[-1] == len(
        legacy_ids
    )

    again_logger = RecordingLogger()
    again = await _service(live_mongo, again_logger, embedders).create_agent(_config())

    assert again.knowledge is not None
    assert _index(live_mongo)["id"] == first_index["id"]  # o índice não foi recriado
    assert not [r for r in again_logger.records if r.level in {"warning", "error"}]


async def test_live_sonda_falha_com_indice_existente_sobe_com_rag_e_a_busca_volta_com_o_provider(
    live_mongo: LiveMongo,
) -> None:
    """Decisão do usuário (rodada 2): sem a sonda, a dimensão vem do índice que a coleção já tem."""
    up = DimensionResolvingEmbedderFactory(_Ollama(), logger=RecordingLogger())
    await _service(live_mongo, RecordingLogger(), up).create_agent(_config())  # startup anterior, provider no ar
    index_before = _index(live_mongo)
    down = _Ollama()
    down.client.error = ConnectionError("recusada")
    logger = RecordingLogger()

    agent = await _service(live_mongo, logger, DimensionResolvingEmbedderFactory(down, logger=logger)).create_agent(
        _config()
    )

    assert agent.knowledge is not None
    assert _index(live_mongo)["id"] == index_before["id"]  # nada recriado
    assert set(_sizes(live_mongo)) == {768}
    assert [r.context.get("dimensions") for r in logger.records if r.level == "warning"] == [768]
    down.client.error = None  # o provider volta
    assert any("segunda via" in c for c in _search_until_found(agent.knowledge, "fatura"))  # type: ignore[arg-type]
