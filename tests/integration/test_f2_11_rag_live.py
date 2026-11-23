"""F2-11, ``live``: o RAG semântico contra um Mongo com Atlas Vector Search de verdade (Atlas Local, F2-10).

Só rodam com ``MONGO_LIVE_URL`` (marker ``live``; nunca na CI padrão); sem ela são pulados. Banco efêmero
por teste (``tests/fakes/live_mongo.py``). O embedder é o ``OllamaEmbedder`` real do agno 3.1.12 com o
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
    """``OllamaEmbedder`` do agno com o cliente do SDK falso (768 posições): sem rede."""

    def __init__(self) -> None:
        self.client = FakeOllamaClient(native_size=768)

    def create_embedder(self, config: ModelConfig) -> Embedder:
        """``dimensions`` só se a config trouxer (``model_params``), como no provider real."""
        kwargs = {"dimensions": config.params["dimensions"]} if "dimensions" in config.params else {}
        return OllamaEmbedder(id=config.model_id, ollama_client=self.client, **kwargs)  # type: ignore[arg-type]


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


def _config(*, dimensions: int | None = None) -> AgentConfig:
    """``dimensions``: ``model_params.dimensions`` explícito (sem sonda), para forçar o vetor errado."""
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


async def test_live_provider_fora_na_indexacao_nao_grava_chunk_e_o_proximo_startup_indexa(
    live_mongo: LiveMongo, caplog: pytest.LogCaptureFixture
) -> None:
    """Agno 3.1.12: a sonda de dimensão responde e o provider cai na indexação. O ``OllamaEmbedder``
    levanta ``EmbeddingError`` (no 2.5.8 devolvia ``[]``) e o ``Knowledge.insert`` o trata (conteúdo
    ``FAILED``): nenhum chunk vai ao Atlas e o agente sobe com o RAG vazio. O próximo startup, com o
    provider de volta, indexa e busca. Sem Atlas: o unit
    ``test_provider_fora_na_indexacao_nao_grava_chunk_e_o_proximo_startup_indexa``.
    """
    falls_after_probe = _Ollama()
    falls_after_probe.client.down_after = 1
    logger = RecordingLogger()
    embedders = DimensionResolvingEmbedderFactory(falls_after_probe, logger=logger)

    agent = await _service(live_mongo, logger, embedders).create_agent(_config())

    assert len(falls_after_probe.client.calls) > 1, "a indexação não chegou ao provider"
    assert agent.knowledge is not None
    assert live_mongo.collection(COLLECTION).count_documents({}) == 0
    assert _num_dimensions(_index(live_mongo)) == 768
    # O ramo do ``EmbeddingError`` no ``Knowledge`` (``agno/knowledge/knowledge.py``) loga o ``safe_message``
    # dele, que começa pelo texto do ``raise_embedding_error`` (``agno/knowledge/embedder/base.py``).
    handled = [r.getMessage() for r in caplog.records if r.name == "agno" and r.levelname == "ERROR"]
    assert any(message.startswith("Error upserting document: Failed to generate embedding") for message in handled)
    assert "conexão recusada" not in repr(logger.records)

    back = DimensionResolvingEmbedderFactory(_Ollama(), logger=RecordingLogger())
    next_startup = await _service(live_mongo, RecordingLogger(), back).create_agent(_config())

    assert next_startup.knowledge is not None
    assert _sizes(live_mongo) and set(_sizes(live_mongo)) == {768}
    assert any("segunda via" in c for c in _search_until_found(next_startup.knowledge, "fatura"))  # type: ignore[arg-type]


async def test_live_embedding_vazio_nunca_e_gravado(live_mongo: LiveMongo) -> None:
    """Vetor de tamanho errado: o ``rag_config`` declara ``dimensions: 1024`` e o modelo devolve 768. O
    ``OllamaEmbedder`` do 3.1.12 devolve ``[]`` (só avisa) e o ``GuardedMongoVectorDb`` recusa o chunk
    antes do Atlas: nenhum documento gravado (nem vazio nem de 768), o agente sobe sem RAG e o erro é o
    nosso, sem o texto do documento. Sem Atlas: o unit
    ``test_embedding_vazio_nunca_e_gravado_e_o_agente_sobe_sem_rag``.
    """
    logger = RecordingLogger()

    agent = await _service(live_mongo, logger, _Ollama()).create_agent(_config(dimensions=1024))

    assert agent.knowledge is None
    assert live_mongo.collection(COLLECTION).count_documents({}) == 0
    [error] = [record for record in logger.records if record.level == "error"]
    assert error.context["error_type"] == "EmbeddingDimensionError"
    assert error.context["rejected"] >= 1
    assert "segunda via" not in repr(logger.records)


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
