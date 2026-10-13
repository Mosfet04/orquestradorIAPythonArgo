"""F2-11 (b) e (c) no RAG semântico: embedding vazio ou de tamanho diferente do índice nunca é gravado,
e a coleção ``rag_<agent_id>`` de antes (embedding vazio, índice com outro ``numDimensions``) é
corrigida no startup.

Montagem de produção (``AgentFactoryService``, ``Knowledge``/``MongoDb`` reais do agno 2.5.8, leitura
e chunking do arquivo de verdade); só o driver síncrono do vector db é o servidor em memória de
``tests/fakes/vector_mongo.py``. Achado O1: o ``MongoDb`` do agno só recusa ``embedding is None``
(``mongodb.py:541-545``) e a indexação usa ``skip_if_exists=True``: um ``[]`` gravado nunca mais era
refeito. O "legado" aqui é gravado pelo próprio agno, como o código de antes do F2-11 fazia.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from agno.knowledge import Knowledge
from agno.knowledge.embedder.azure_openai import AzureOpenAIEmbedder
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
from tests.fakes import FakeEmbedderFactory, FakeModelFactory, InMemoryToolRepository, RecordingLogger
from tests.fakes.embedders import FakeAzureOpenAIClient, FakeOllamaClient, ScriptedEmbedder
from tests.fakes.session_db import fake_session_db
from tests.fakes.vector_mongo import FakeVectorCollection, FakeVectorServer

DB_URL = "mongodb://mongo.test.invalid:27017"
DB_NAME = "test_db"
AGENT_ID = "suporte"
COLLECTION = rag_collection_name(AGENT_ID)
DOC_MARKER = "MARCADOR-DO-TEXTO-DO-DOCUMENTO"
# Duas seções grandes: o MarkdownReader do agno (chunks de 5000) gera 2 chunks; o 1º tem a "Seção 0".
BIG_DOC = "# Manual\n\n" + "".join(
    f"## Seção {i}\n\n" + (f"Parágrafo {i} sobre faturas {DOC_MARKER}. " * 30) + "\n\n" for i in range(4)
)


class _Factory(IEmbedderFactory):
    def __init__(self, make: Any) -> None:
        self._make = make

    def create_embedder(self, config: ModelConfig) -> Embedder:
        embedder: Embedder = self._make(config)
        return embedder


def _ollama(native_size: int = 768, error: Exception | None = None) -> _Factory:
    """``OllamaEmbedder`` real do agno, com o cliente do SDK falso; ``dimensions`` só se a config trouxer."""
    client = FakeOllamaClient(native_size=native_size, error=error)

    def make(config: ModelConfig) -> Embedder:
        kwargs = {"dimensions": config.params["dimensions"]} if "dimensions" in config.params else {}
        return OllamaEmbedder(id=config.model_id, ollama_client=client, **kwargs)  # type: ignore[arg-type]

    return _Factory(make)


def _scripted(dimensions: int, size_for: Any) -> _Factory:
    return _Factory(lambda config: ScriptedEmbedder(dimensions=dimensions, size_for=size_for))


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


def _config(agent_id: str = AGENT_ID, *, dimensions: int | None = None) -> AgentConfig:
    """``dimensions``: ``model_params.dimensions`` explícito (sem sonda), para forçar vetor errado."""
    return AgentConfig(
        id=agent_id,
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
    (docs / "manual.md").write_text(BIG_DOC, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    return docs


def _collection(server: FakeVectorServer, name: str = COLLECTION) -> FakeVectorCollection:
    return server.collection(DB_NAME, name)


def _sizes(collection: FakeVectorCollection) -> list[int]:
    return sorted(len(doc["embedding"]) for doc in collection.docs.values())


def _index_dimensions(collection: FakeVectorCollection) -> list[int]:
    return [i["latestDefinition"]["fields"][0]["numDimensions"] for i in collection.search_indexes]


def _write_legacy(name: str = COLLECTION) -> None:
    """O que o código de antes do F2-11 gravava com o ``rag_config`` legado do Ollama (O1 + O2)."""
    client = FakeOllamaClient(native_size=768)
    knowledge = Knowledge(
        vector_db=MongoVectorDb(
            collection_name=name,
            db_url=DB_URL,
            database=DB_NAME,
            embedder=OllamaEmbedder(id="nomic-embed-text:latest", ollama_client=client),  # type: ignore[arg-type]
        )
    )
    knowledge.insert(path="docs/manual.md", skip_if_exists=True)


def _no_document_text(logger: RecordingLogger) -> None:
    assert DOC_MARKER not in repr(logger.records)


# ── (b) nada de embedding inválido gravado ─────────────────────────────


async def test_o1_reproduzido_o_agno_sozinho_grava_embedding_vazio(fake_vector_mongo: FakeVectorServer) -> None:
    _write_legacy()

    collection = _collection(fake_vector_mongo)
    assert collection.docs and _sizes(collection) == [0, 0]  # o defeito: [] gravado
    assert _index_dimensions(collection) == [4096]  # e o índice com a dimensão errada


async def test_embedding_vazio_nunca_e_gravado_e_o_agente_sobe_sem_rag(fake_vector_mongo: FakeVectorServer) -> None:
    """``dimensions`` explícito errado (1024; o modelo devolve 768): o ``OllamaEmbedder`` devolve ``[]``."""
    logger = RecordingLogger()

    agent = await _service(logger, _ollama()).create_agent(_config(dimensions=1024))

    assert agent.knowledge is None
    assert _collection(fake_vector_mongo).docs == {}
    [record] = [r for r in logger.records if r.level == "error"]
    assert record.context == {
        "agent_id": AGENT_ID,
        "collection": COLLECTION,
        "rejected": 2,
        "removed": 0,
        "error_type": "EmbeddingDimensionError",
    }
    _no_document_text(logger)


async def test_embedding_de_tamanho_diferente_do_indice_nunca_e_gravado(fake_vector_mongo: FakeVectorServer) -> None:
    logger = RecordingLogger()

    agent = await _service(logger, _scripted(8, lambda _text: 4)).create_agent(_config(dimensions=8))

    assert agent.knowledge is None
    assert _collection(fake_vector_mongo).docs == {}
    assert [r.context["rejected"] for r in logger.records if r.level == "error"] == [2]
    _no_document_text(logger)


async def test_chunk_recusado_descarta_o_documento_e_o_proximo_startup_reindexa(
    fake_vector_mongo: FakeVectorServer,
) -> None:
    logger = RecordingLogger()
    only_first = _scripted(8, lambda text: None if "Seção 0" in text else 0)

    agent = await _service(logger, only_first).create_agent(_config(dimensions=8))

    # o chunk bom não fica sozinho: com skip_if_exists o documento nunca mais seria completado
    assert agent.knowledge is None
    assert _collection(fake_vector_mongo).docs == {}
    [record] = [r for r in logger.records if r.level == "error"]
    assert (record.context["rejected"], record.context["removed"]) == (1, 1)

    again = await _service(RecordingLogger(), FakeEmbedderFactory(dimensions=8)).create_agent(_config())

    assert again.knowledge is not None
    assert _sizes(_collection(fake_vector_mongo)) == [8, 8]


async def test_com_a_sonda_o_legado_do_ollama_grava_768_com_indice_768(fake_vector_mongo: FakeVectorServer) -> None:
    logger = RecordingLogger()
    embedders = DimensionResolvingEmbedderFactory(_ollama(native_size=768), logger=logger)

    agent = await _service(logger, embedders).create_agent(_config())

    collection = _collection(fake_vector_mongo)
    assert agent.knowledge is not None
    assert _sizes(collection) == [768, 768]
    assert _index_dimensions(collection) == [768]
    assert not [r for r in logger.records if r.level in {"warning", "error"}]
    assert not [op for op, on_loop in collection.ops if on_loop]  # Mongo síncrono fora do event loop


async def test_falha_da_sonda_sobe_o_agente_sem_rag_e_sem_criar_colecao(fake_vector_mongo: FakeVectorServer) -> None:
    logger = RecordingLogger()
    embedders = DimensionResolvingEmbedderFactory(_ollama(error=ConnectionError("recusada")), logger=logger)

    agent = await _service(logger, embedders).create_agent(_config())

    assert agent.knowledge is None
    assert fake_vector_mongo.created == set()
    assert [(r.level, r.context["error_type"]) for r in logger.records if "error_type" in r.context] == [
        ("error", "ConnectionError"),
        ("error", "EmbedderProbeError"),
    ]


# ── (c) coleção legada corrigida no startup ───────────────────────────


async def test_colecao_legada_com_embedding_vazio_e_indice_errado_e_corrigida_no_startup(
    fake_vector_mongo: FakeVectorServer,
) -> None:
    _write_legacy()
    _write_legacy("rag_outro")  # coleção de outro agente: não é tocada
    other_before = dict(_collection(fake_vector_mongo, "rag_outro").docs)
    legacy_ids = set(_collection(fake_vector_mongo).docs)
    logger = RecordingLogger()
    embedders = DimensionResolvingEmbedderFactory(_ollama(native_size=768), logger=logger)

    agent = await _service(logger, embedders).create_agent(_config())

    collection = _collection(fake_vector_mongo)
    assert agent.knowledge is not None
    assert set(collection.docs) == legacy_ids  # mesmos chunks, reindexados no lugar (nada apagado)
    assert _sizes(collection) == [768, 768]
    assert collection.dropped_search_indexes == ["vector_index_1"]
    assert _index_dimensions(collection) == [768]
    warnings = {r.message: r.context for r in logger.records if r.level == "warning"}
    assert {"agent_id": AGENT_ID, "collection": COLLECTION, "previous_dimensions": 4096, "dimensions": 768} in (
        warnings.values()
    )
    assert {"agent_id": AGENT_ID, "collection": COLLECTION, "reembedded": 2} in warnings.values()
    assert not [r for r in logger.records if r.level == "error"]
    assert _collection(fake_vector_mongo, "rag_outro").docs == other_before
    _no_document_text(logger)


async def test_correcao_da_colecao_legada_e_idempotente(fake_vector_mongo: FakeVectorServer) -> None:
    _write_legacy()
    embedders = DimensionResolvingEmbedderFactory(_ollama(native_size=768), logger=RecordingLogger())
    await _service(RecordingLogger(), embedders).create_agent(_config())
    collection = _collection(fake_vector_mongo)
    snapshot = {key: dict(doc) for key, doc in collection.docs.items()}
    collection.ops.clear()
    logger = RecordingLogger()

    agent = await _service(logger, embedders).create_agent(_config())

    assert agent.knowledge is not None
    assert collection.docs == snapshot
    assert collection.dropped_search_indexes == ["vector_index_1"]  # só o do 1º startup
    writes = {"update_one", "insert_many", "delete_many", "create_search_index", "drop_search_index"}
    assert not [op for op, _ in collection.ops if op in writes]
    assert not [r for r in logger.records if r.level in {"warning", "error"}]


async def test_colecao_sem_indice_ganha_o_indice_no_startup(fake_vector_mongo: FakeVectorServer) -> None:
    fake_vector_mongo.seed(DB_NAME, COLLECTION, [])  # ex.: criada num mongo sem vector search
    logger = RecordingLogger()

    agent = await _service(logger, FakeEmbedderFactory(dimensions=8)).create_agent(_config())

    assert agent.knowledge is not None
    assert _index_dimensions(_collection(fake_vector_mongo)) == [8]
    assert [r.context.get("previous_dimensions") for r in logger.records if r.level == "warning"] == [None]


async def test_reindexacao_que_falha_sobe_sem_rag_sem_apagar_os_chunks(fake_vector_mongo: FakeVectorServer) -> None:
    _write_legacy()
    before = {key: dict(doc) for key, doc in _collection(fake_vector_mongo).docs.items()}
    logger = RecordingLogger()

    agent = await _service(logger, _scripted(8, lambda _text: 0)).create_agent(_config(dimensions=8))

    assert agent.knowledge is None
    assert _collection(fake_vector_mongo).docs == before  # nada apagado; o próximo startup tenta de novo
    [record] = [r for r in logger.records if r.level == "error"]
    assert record.context == {
        "agent_id": AGENT_ID,
        "collection": COLLECTION,
        "failed": 2,
        "error_type": "EmbeddingDimensionError",
    }
    _no_document_text(logger)


async def test_indice_sem_campo_vetorial_e_recriado(fake_vector_mongo: FakeVectorServer) -> None:
    collection = fake_vector_mongo.seed(DB_NAME, COLLECTION, [])
    collection.search_indexes.append({"name": "vector_index_1", "type": "search", "latestDefinition": {"fields": []}})
    logger = RecordingLogger()

    agent = await _service(logger, FakeEmbedderFactory(dimensions=8)).create_agent(_config())

    assert agent.knowledge is not None
    assert collection.dropped_search_indexes == ["vector_index_1"]
    assert _index_dimensions(collection) == [8]


# ── rodada 2: R1 (pedido do provider intacto) e fallback para o índice existente ──


async def test_azure_ada_002_indexa_com_a_dimensao_da_sonda_sem_pedir_dimensions(
    fake_vector_mongo: FakeVectorServer,
) -> None:
    client = FakeAzureOpenAIClient(native_size=1536)
    azure = _Factory(lambda config: AzureOpenAIEmbedder(id="text-embedding-ada-002", openai_client=client))
    logger = RecordingLogger()

    agent = await _service(logger, DimensionResolvingEmbedderFactory(azure, logger=logger)).create_agent(_config())

    collection = _collection(fake_vector_mongo)
    assert agent.knowledge is not None
    assert _index_dimensions(collection) == [1536]
    assert _sizes(collection) == [1536, 1536]
    assert client.requests and not [r for r in client.requests if "dimensions" in r]  # o 400 do ada-002


def _seed_indexed(server: FakeVectorServer, dimensions: int = 768) -> FakeVectorCollection:
    """Coleção do agente já indexada (startup anterior, provider no ar)."""
    docs = [
        {"_id": "c1", "name": "manual.md", "content": "Parágrafo sobre faturas", "content_hash": "h",
         "embedding": [0.1] * dimensions},
    ]  # fmt: skip
    return server.seed(DB_NAME, COLLECTION, docs, index_dimensions=dimensions)


async def test_sonda_falha_com_indice_existente_sobe_com_rag_pela_dimensao_do_indice(
    fake_vector_mongo: FakeVectorServer,
) -> None:
    collection = _seed_indexed(fake_vector_mongo)
    client = FakeOllamaClient(native_size=768, error=ConnectionError("recusada"))
    ollama = _Factory(lambda config: OllamaEmbedder(id=config.model_id, ollama_client=client))  # type: ignore[arg-type]
    logger = RecordingLogger()
    before = {key: dict(doc) for key, doc in collection.docs.items()}

    agent = await _service(logger, DimensionResolvingEmbedderFactory(ollama, logger=logger)).create_agent(_config())

    assert agent.knowledge is not None
    assert collection.docs == before  # nada gravado nem apagado com o provider fora
    writes = {"update_one", "insert_many", "delete_many", "create_search_index", "drop_search_index"}
    assert not [op for op, _ in collection.ops if op in writes]
    [warning] = [r for r in logger.records if r.level == "warning"]
    assert warning.context == {
        "agent_id": AGENT_ID,
        "collection": COLLECTION,
        "dimensions": 768,
        "error_type": "ConnectionError",
    }
    # o provider volta: a busca embeda a pergunta com o tamanho do índice (não o 4096 do agno)
    client.error = None
    embedder = agent.knowledge.vector_db.embedder  # type: ignore[union-attr]
    assert len(embedder.get_embedding("fatura")) == 768


async def test_sonda_falha_sem_indice_sobe_sem_rag_e_nao_cria_colecao(fake_vector_mongo: FakeVectorServer) -> None:
    fake_vector_mongo.seed(DB_NAME, COLLECTION, [])  # coleção sem índice vetorial: nada em que se apoiar
    client = FakeOllamaClient(error=ConnectionError("recusada"))
    ollama = _Factory(lambda config: OllamaEmbedder(id=config.model_id, ollama_client=client))  # type: ignore[arg-type]
    logger = RecordingLogger()

    agent = await _service(logger, DimensionResolvingEmbedderFactory(ollama, logger=logger)).create_agent(_config())

    assert agent.knowledge is None
    assert _collection(fake_vector_mongo).search_indexes == []
    assert [(r.level, r.context.get("error_type")) for r in logger.records if r.level != "info"] == [
        ("error", "ConnectionError"),
        ("error", "EmbedderProbeError"),
    ]
