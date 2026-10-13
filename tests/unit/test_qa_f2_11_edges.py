"""QA do F2-11: bordas que os testes do dev não cobrem.

- ``dimensions`` explícito vindo do Mongo/YAML em formatos reais (double ``768.0``, zero) e documentos
  legados de ``rag_config`` (sem ``model_params``) pela porta de config;
- reparo da coleção: chunk com vetor maior/menor que o índice, ausente, ``None``, sem ``content``; índice
  já certo com vetor vazio; remoção assíncrona do índice (some depois de algumas consultas) e remoção que
  nunca termina (agente sobe sem RAG, nada apagado);
- sonda: uma por config mesmo com agentes em ``gather``; ``base_url`` diferente é outra config; resposta
  sem embedding;
- hierárquico: reindexações concorrentes da árvore velha viram uma, nó sem texto (``None``) não conta
  como legado, falha ao gravar depois de apagar é refeita no próximo startup;
- segurança: nenhum log do agno nem nosso leva o texto do documento ou o da exceção.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterator, Mapping
from pathlib import Path
from typing import Any

import pytest
from agno.knowledge.embedder.base import Embedder
from agno.knowledge.embedder.ollama import OllamaEmbedder

from src.application.services.document_indexing_service import DocumentIndexingService
from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.document_node import DocumentNode
from src.domain.entities.model_config import ModelConfig
from src.domain.entities.rag_config import RagConfig, SearchStrategy
from src.domain.ports.embedder_factory_port import IEmbedderFactory
from src.domain.ports.summary_generator_port import ISummaryGenerator
from src.infrastructure.parsers.text_document_parser import TextDocumentParser
from src.infrastructure.repositories.config_documents import map_rag_document
from src.infrastructure.runtime.agno import rag_vector_db
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService, rag_collection_name
from src.infrastructure.runtime.agno.embedder_dimensions import (
    DimensionResolvingEmbedderFactory,
    EmbedderProbeError,
)
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from tests.fakes import FakeModelFactory, InMemoryDocumentTreeRepository, InMemoryToolRepository, RecordingLogger
from tests.fakes.embedders import FakeOllamaClient, ScriptedEmbedder, unit_vector
from tests.fakes.session_db import fake_session_db
from tests.fakes.vector_mongo import FakeVectorCollection, FakeVectorServer

DB_URL = "mongodb://mongo.test.invalid:27017"
DB_NAME = "test_db"
DOC_MARKER = "MARCADOR-DO-TEXTO-DO-DOCUMENTO"
SDK_TEXT = "texto-da-excecao-do-sdk-com-segredo"


class _Factory(IEmbedderFactory):
    def __init__(self, make: Callable[[ModelConfig], Embedder]) -> None:
        self._make = make

    def create_embedder(self, config: ModelConfig) -> Embedder:
        return self._make(config)


def _ollama(client: FakeOllamaClient) -> _Factory:
    def make(config: ModelConfig) -> Embedder:
        kwargs = {"dimensions": config.params["dimensions"]} if "dimensions" in config.params else {}
        return OllamaEmbedder(id=config.model_id, ollama_client=client, **kwargs)  # type: ignore[arg-type]

    return _Factory(make)


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


def _agent(agent_id: str, *, doc_name: str | None = None, params: Mapping[str, Any] | None = None) -> AgentConfig:
    return AgentConfig(
        id=agent_id,
        nome=agent_id,
        factory_ia_model="ollama",
        model="llama3.2:latest",
        descricao="d",
        prompt="p",
        rag_config=RagConfig(
            active=True,
            doc_name=doc_name,
            factory_ia_model="ollama",
            model="nomic-embed-text:latest",
            model_params=params,  # type: ignore[arg-type]
        ),
    )


def _collection(server: FakeVectorServer, agent_id: str) -> FakeVectorCollection:
    return server.collection(DB_NAME, rag_collection_name(agent_id))


def _chunk(chunk_id: str, content: object, embedding: object) -> dict[str, Any]:
    doc: dict[str, Any] = {"_id": chunk_id, "name": "manual", "content": content, "meta_data": {}}
    if embedding is not ...:
        doc["embedding"] = embedding
    return doc


def _sizes(collection: FakeVectorCollection) -> dict[str, int]:
    return {str(k): len(d["embedding"]) for k, d in collection.docs.items()}


@pytest.fixture(autouse=True)
def _env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "manual.md").write_text(f"# M\n\n## A\n\nTexto {DOC_MARKER}.\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")


# ── dimensions explícito em formatos reais ──────────────────────────


def test_documento_legado_de_rag_sem_model_params_continua_valido_e_ganha_a_sonda() -> None:
    config = map_rag_document({"active": True, "doc_name": "manual.md"}).embedder_model_config()
    client = FakeOllamaClient(native_size=768)

    embedder = DimensionResolvingEmbedderFactory(_ollama(client), logger=RecordingLogger()).create_embedder(config)

    assert (config.provider, config.model_id, dict(config.params)) == ("ollama", "nomic-embed-text:latest", {})
    assert embedder.dimensions == 768  # type: ignore[attr-defined]


async def test_dimensions_double_do_mongo_sobe_com_rag(fake_vector_mongo: FakeVectorServer) -> None:
    """``768`` digitado no mongosh/Compass chega como ``float`` (double): é o mesmo 768 do int."""
    config = map_rag_document(
        {
            "active": True,
            "doc_name": "manual.md",
            "factoryIaModel": "ollama",
            "model": "nomic-embed-text:latest",
            "model_params": {"dimensions": 768.0},
        }
    )
    assert config.model_params == {"dimensions": 768.0}
    client = FakeOllamaClient(native_size=768)
    logger = RecordingLogger()
    embedders = DimensionResolvingEmbedderFactory(_ollama(client), logger=logger)
    agent_config = AgentConfig(
        id="dbl", nome="d", factory_ia_model="ollama", model="llama3.2:latest", descricao="d", prompt="p",
        rag_config=config,
    )  # fmt: skip

    agent = await _service(logger, embedders).create_agent(agent_config)

    assert agent.knowledge is not None
    assert [r for r in logger.records if r.level in {"warning", "error"}] == []
    assert set(_sizes(_collection(fake_vector_mongo, "dbl")).values()) == {768}


async def test_dimensions_zero_explicito_sobe_sem_rag_com_log_e_sem_gravar(fake_vector_mongo: FakeVectorServer) -> None:
    logger = RecordingLogger()
    embedders = DimensionResolvingEmbedderFactory(_ollama(FakeOllamaClient()), logger=logger)

    config = _agent("zero", doc_name="manual.md", params={"dimensions": 0})

    agent = await _service(logger, embedders).create_agent(config)

    assert agent.knowledge is None
    assert _collection(fake_vector_mongo, "zero").docs == {}
    assert [r.context["error_type"] for r in logger.records if r.level in {"warning", "error"}] == [
        "EmbeddingDimensionError"
    ]


# ── reparo da coleção ────────────────────────────────────────────────


async def test_reparo_so_reindexa_o_que_esta_errado_e_preserva_o_valido(fake_vector_mongo: FakeVectorServer) -> None:
    good = unit_vector("bom", 8)
    collection = fake_vector_mongo.seed(
        DB_NAME,
        rag_collection_name("misto"),
        [
            _chunk("ok", "texto bom", good),
            _chunk("vazio", "texto vazio", []),
            _chunk("curto", "texto curto", [0.1] * 4),
            _chunk("longo", "texto longo", [0.1] * 16),
            _chunk("ausente", "texto ausente", ...),
            _chunk("nulo", "texto nulo", None),
        ],
        index_dimensions=8,
    )
    embedder = ScriptedEmbedder(dimensions=8)
    logger = RecordingLogger()

    # dimensions explícito: sem sonda, as chamadas ao embedder são só as do reparo (F2-11 rodada 2)
    agent = await _service(logger, _Factory(lambda _c: embedder)).create_agent(
        _agent("misto", params={"dimensions": 8})
    )

    assert agent.knowledge is not None
    assert _sizes(collection) == {k: 8 for k in ("ok", "vazio", "curto", "longo", "ausente", "nulo")}
    assert collection.docs["ok"]["embedding"] == good  # válido: intocado
    assert sorted(embedder.calls) == ["texto ausente", "texto curto", "texto longo", "texto nulo", "texto vazio"]
    assert collection.dropped_search_indexes == [] and collection.created_search_indexes == []  # índice já certo
    [info] = [r.context for r in logger.records if r.level == "warning"]
    assert info == {"agent_id": "misto", "collection": rag_collection_name("misto"), "reembedded": 5}


async def test_chunk_sem_content_nao_apaga_nada_e_deixa_o_agente_sem_rag(fake_vector_mongo: FakeVectorServer) -> None:
    collection = fake_vector_mongo.seed(
        DB_NAME,
        rag_collection_name("semtexto"),
        [_chunk("a", "texto a", []), _chunk("b", None, []), _chunk("c", "", [])],
        index_dimensions=8,
    )
    logger = RecordingLogger()

    agent = await _service(logger, _Factory(lambda _c: ScriptedEmbedder(dimensions=8))).create_agent(_agent("semtexto"))

    assert agent.knowledge is None
    assert set(collection.docs) == {"a", "b", "c"}  # nada apagado
    assert len(collection.docs["a"]["embedding"]) == 8  # o que dava para corrigir foi corrigido
    [error] = [r.context for r in logger.records if r.level == "error"]
    assert error["failed"] == 2 and error["error_type"] == "EmbeddingDimensionError"


async def test_indice_ja_certo_com_vetores_vazios_so_reindexa_os_chunks(fake_vector_mongo: FakeVectorServer) -> None:
    collection = fake_vector_mongo.seed(
        DB_NAME, rag_collection_name("so_vazio"), [_chunk("a", "texto a", [])], index_dimensions=8
    )
    logger = RecordingLogger()

    agent = await _service(logger, _Factory(lambda _c: ScriptedEmbedder(dimensions=8))).create_agent(_agent("so_vazio"))

    assert agent.knowledge is not None
    assert _sizes(collection) == {"a": 8}
    assert collection.dropped_search_indexes == []
    assert [r.message for r in logger.records if r.level == "warning"] == [
        "Chunks do RAG com embedding vazio ou de dimensão diferente reindexados"
    ]


async def test_indice_removido_de_forma_assincrona_e_esperado_ate_sumir(
    fake_vector_mongo: FakeVectorServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No Atlas o índice segue listado por um tempo depois do ``drop``; criar antes falharia."""
    monkeypatch.setattr(rag_vector_db, "_INDEX_POLL_SECONDS", 0.0)
    collection = fake_vector_mongo.seed(DB_NAME, rag_collection_name("lento"), [], index_dimensions=4096)
    lingering = {"polls": 3}
    real_drop = FakeVectorCollection.drop_search_index
    real_list = FakeVectorCollection.list_search_indexes
    state: dict[str, Any] = {"asked_drop": False, "kept": []}

    def drop(self: FakeVectorCollection, name: str) -> None:
        state["asked_drop"] = True
        state["kept"] = list(self.search_indexes)  # ainda listado
        self._use("drop_search_index")
        self.dropped_search_indexes.append(name)

    def list_indexes(self: FakeVectorCollection, name: str | None = None, **kw: Any) -> Iterator[dict[str, Any]]:
        if state["asked_drop"] and lingering["polls"] > 0:
            lingering["polls"] -= 1
            return iter(state["kept"])
        if state["asked_drop"]:
            state["asked_drop"] = False  # o drop terminou: daqui em diante a listagem é a real
            self.search_indexes = []
        return real_list(self, name, **kw)

    monkeypatch.setattr(FakeVectorCollection, "drop_search_index", drop)
    monkeypatch.setattr(FakeVectorCollection, "list_search_indexes", list_indexes)
    created_after_gone: list[int] = []
    real_create = FakeVectorCollection.create_search_index

    def create(self: FakeVectorCollection, model: Any) -> str:
        created_after_gone.append(lingering["polls"])
        return real_create(self, model)

    monkeypatch.setattr(FakeVectorCollection, "create_search_index", create)
    assert real_drop is not None

    agent = await _service(RecordingLogger(), _Factory(lambda _c: ScriptedEmbedder(dimensions=8))).create_agent(
        _agent("lento")
    )

    assert agent.knowledge is not None
    assert created_after_gone == [0]  # só criou depois de o índice velho sumir da listagem
    assert [i["latestDefinition"]["fields"][0]["numDimensions"] for i in collection.search_indexes] == [8]


async def test_indice_que_nunca_some_derruba_so_o_rag_e_nao_apaga_nada(
    fake_vector_mongo: FakeVectorServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(rag_vector_db, "_INDEX_POLL_SECONDS", 0.001)
    monkeypatch.setattr(rag_vector_db, "_INDEX_DROP_TIMEOUT_SECONDS", 0.02)
    collection = fake_vector_mongo.seed(
        DB_NAME, rag_collection_name("preso"), [_chunk("a", f"texto {DOC_MARKER}", [])], index_dimensions=4096
    )
    monkeypatch.setattr(FakeVectorCollection, "drop_search_index", lambda self, name: self._use("drop_search_index"))
    logger = RecordingLogger()

    agent = await _service(logger, _Factory(lambda _c: ScriptedEmbedder(dimensions=8))).create_agent(_agent("preso"))

    assert agent.knowledge is None
    assert set(collection.docs) == {"a"} and collection.docs["a"]["embedding"] == []
    assert collection.created_search_indexes == []
    [record] = [r for r in logger.records if r.level == "warning"]
    assert record.context == {"agent_id": "preso", "error_type": "TimeoutError"}
    assert DOC_MARKER not in repr(logger.records)


# ── sonda ────────────────────────────────────────────────────────────


async def test_agentes_em_paralelo_com_o_mesmo_embedder_fazem_uma_sonda(fake_vector_mongo: FakeVectorServer) -> None:
    client = FakeOllamaClient(native_size=768)
    logger = RecordingLogger()
    service = _service(logger, DimensionResolvingEmbedderFactory(_ollama(client), logger=logger))

    agents = await asyncio.gather(*(service.create_agent(_agent(f"par{i}", doc_name="manual.md")) for i in range(4)))

    assert all(a.knowledge is not None for a in agents)
    assert [c["input"] for c in client.calls].count("dimensão") == 1
    assert [set(_sizes(_collection(fake_vector_mongo, f"par{i}")).values()) for i in range(4)] == [{768}] * 4


def test_base_url_diferente_e_outra_config_e_outra_sonda() -> None:
    client = FakeOllamaClient(native_size=768)
    factory = DimensionResolvingEmbedderFactory(_ollama(client), logger=RecordingLogger())
    a = ModelConfig(provider="ollama", model_id="nomic-embed-text:latest", base_url="http://ollama-a.example.com:11434")
    b = ModelConfig(provider="ollama", model_id="nomic-embed-text:latest", base_url="http://ollama-b.example.com:11434")

    factory.create_embedder(a)
    factory.create_embedder(b)
    factory.create_embedder(a)

    assert len(client.calls) == 2


def test_resposta_sem_nenhum_embedding_e_falha_da_sonda() -> None:
    class _NoEmbeddings(FakeOllamaClient):
        def embed(self, model: str = "", input: str = "", **kwargs: Any) -> Any:  # pylint: disable=redefined-builtin
            return {"embeddings": []}

    logger = RecordingLogger()
    factory = DimensionResolvingEmbedderFactory(_ollama(_NoEmbeddings()), logger=logger)

    with pytest.raises(EmbedderProbeError):
        factory.create_embedder(ModelConfig(provider="ollama", model_id="nomic-embed-text:latest"))

    assert [r.context["error_type"] for r in logger.records if r.level == "error"] == ["EmptyEmbeddingError"]


# ── hierárquico ──────────────────────────────────────────────────────

DOC = f"# T\n\nIntro {DOC_MARKER}.\n\n## A\n\nTexto A.\n\n## B\n\nTexto B.\n"
RAG = RagConfig(active=True, doc_name="m.md", search_strategy=SearchStrategy.HIERARCHICAL)


class _Summary(ISummaryGenerator):
    async def generate_summary(self, content: str) -> str:
        return "resumo"


def _indexing(tree: InMemoryDocumentTreeRepository, embedders: IEmbedderFactory, logger: RecordingLogger) -> Any:
    return DocumentIndexingService(
        parser=TextDocumentParser(),
        tree_repository=tree,
        summary_generator=_Summary(),
        embedder_factory=embedders,
        logger=logger,
    )


async def _legacy_tree(tree: InMemoryDocumentTreeRepository) -> None:
    nodes = TextDocumentParser().parse(DOC, "m.md")
    for node in nodes:
        node.embedding = []
    await tree.save_nodes(nodes)


async def test_reindexacoes_concorrentes_da_arvore_legada_viram_uma() -> None:
    tree = InMemoryDocumentTreeRepository()
    await _legacy_tree(tree)
    embedder = ScriptedEmbedder(dimensions=8)
    service = _indexing(tree, _Factory(lambda _c: embedder), RecordingLogger())

    results = await asyncio.gather(*(service.index_document("m.md", DOC, RAG) for _ in range(3)))

    assert sorted(len(r) for r in results) == [0, 0, 3]
    assert len(embedder.calls) == 3  # uma passada de embeddings, não três
    assert [len(n.embedding or []) for n in tree._nodes] == [8, 8, 8]


async def test_no_sem_texto_com_embedding_none_nao_conta_como_arvore_legada() -> None:
    tree = InMemoryDocumentTreeRepository()
    ok = DocumentNode(id="m::0", doc_name="m.md", level=0, title="T", content="texto", embedding=[0.1] * 8)
    empty = DocumentNode(id="m::1", doc_name="m.md", level=1, title="vazio", content="", embedding=None)
    await tree.save_nodes([ok, empty])
    embedder = ScriptedEmbedder(dimensions=8)

    nodes = await _indexing(tree, _Factory(lambda _c: embedder), RecordingLogger()).index_document("m.md", DOC, RAG)

    assert nodes == [] and embedder.calls == []


async def test_falha_ao_gravar_depois_de_apagar_a_arvore_velha_e_refeita_na_proxima_indexacao() -> None:
    class _FailingSave(InMemoryDocumentTreeRepository):
        fail = True

        async def save_nodes(self, nodes: list[DocumentNode]) -> None:
            if self.fail:
                raise OSError("disco cheio")
            await super().save_nodes(nodes)

    tree = _FailingSave()
    tree.fail = False
    await _legacy_tree(tree)
    tree.fail = True
    embedders = _Factory(lambda _c: ScriptedEmbedder(dimensions=8))
    service = _indexing(tree, embedders, RecordingLogger())

    with pytest.raises(OSError):
        await service.index_document("m.md", DOC, RAG)
    assert await tree.exists("m.md") is False  # nada de árvore pela metade: o próximo startup refaz

    tree.fail = False
    nodes = await service.index_document("m.md", DOC, RAG)

    assert len(nodes) == 3 and [len(n.embedding or []) for n in tree._nodes] == [8, 8, 8]


# ── segurança: nenhum log (nosso ou do agno) leva texto do documento nem da exceção ─────────────────


class _Capture(logging.Handler):
    def __init__(self) -> None:
        super().__init__(logging.DEBUG)
        self.lines: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.lines.append(record.getMessage())


@pytest.mark.parametrize(
    "scenario", ["embedding-vazio", "tamanho-errado", "sonda-fora-do-ar", "reparo-que-falha"]
)
async def test_nenhum_log_leva_o_texto_do_documento_nem_o_da_excecao(
    scenario: str, fake_vector_mongo: FakeVectorServer
) -> None:
    capture = _Capture()
    agno_logger = logging.getLogger("agno")
    agno_logger.addHandler(capture)
    previous = agno_logger.level
    agno_logger.setLevel(logging.DEBUG)
    logger = RecordingLogger()
    # Rodada 2: a fábrica de agente sempre resolve a dimensão; vetor errado vem de dimensions explícito.
    params: Mapping[str, Any] | None = {"dimensions": 8}
    try:
        if scenario == "embedding-vazio":
            params = {"dimensions": 1024}  # o modelo devolve 768: o OllamaEmbedder do agno devolve []
            embedders: IEmbedderFactory = _ollama(FakeOllamaClient(native_size=768))
        elif scenario == "tamanho-errado":
            embedders = _Factory(lambda _c: ScriptedEmbedder(dimensions=8, size_for=lambda _t: 4))
        elif scenario == "sonda-fora-do-ar":
            params = None
            client = FakeOllamaClient(error=ConnectionError(f"{SDK_TEXT} {DOC_MARKER}"))
            embedders = DimensionResolvingEmbedderFactory(_ollama(client), logger=logger)
        else:
            fake_vector_mongo.seed(
                DB_NAME, rag_collection_name("seg"), [_chunk("a", f"texto {DOC_MARKER}", [])], index_dimensions=8
            )
            embedders = _Factory(lambda _c: ScriptedEmbedder(dimensions=8, size_for=lambda _t: 0))

        agent = await _service(logger, embedders).create_agent(_agent("seg", doc_name="manual.md", params=params))
    finally:
        agno_logger.removeHandler(capture)
        agno_logger.setLevel(previous)

    assert agent.knowledge is None
    everything = repr(logger.records) + "\n".join(capture.lines)
    assert DOC_MARKER not in everything
    assert SDK_TEXT not in everything
