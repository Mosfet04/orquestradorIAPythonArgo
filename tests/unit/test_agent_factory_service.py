"""``AgentFactoryService``: o ``Agent`` real do agno 2.5 montado a partir de ``AgentConfig``.

Sem ``patch`` de ``Agent``/``Knowledge``/``MongoDb`` no módulo da fábrica: a montagem é a de produção
com fakes nas portas (modelo, embedder, repositório de tools) e o I/O do agno cortado na borda pela
fixture ``offline_knowledge`` (cliente Mongo sem conexão, ``Knowledge.insert`` só registrado). O db de
sessões é o ``MongoDb`` real do agno, que só conecta no primeiro uso; o F2-08 passa a injetá-lo.

O que já é coberto em outro lugar não se repete aqui: kwargs completos de ``Agent`` (``tests/golden``),
tools (``test_agent_factory_tools.py``), guardrail de ``user_id`` (``test_qa_f1_06_guardrail.py``),
coleção do RAG (``test_rag_collection_per_agent.py``) e ``doc_name`` (``tests/security/test_rag_doc_path.py``).
"""

from __future__ import annotations

from pathlib import Path

import pytest
from agno.db.mongo import MongoDb
from agno.knowledge import Knowledge

from src.application.services.document_indexing_service import DocumentIndexingService
from src.application.services.knowledge_search_factory import KnowledgeSearchFactory
from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.rag_config import RagConfig, SearchStrategy
from src.domain.ports.model_factory_port import InvalidModelConfigError
from src.domain.ports.summary_generator_port import ISummaryGenerator
from src.infrastructure.parsers.text_document_parser import TextDocumentParser
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService, rag_collection_name
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from tests.fakes import (
    FakeEmbedderFactory,
    FakeModelFactory,
    InMemoryDocumentTreeRepository,
    InMemoryToolRepository,
    RecordingLogger,
)
from tests.fakes.knowledge import OfflineKnowledge

pytestmark = pytest.mark.usefixtures("offline_knowledge")

DB_URL = "mongodb://mongo.test.invalid:27017"
DB_NAME = "test_db"


class _FixedSummary(ISummaryGenerator):
    async def generate_summary(self, content: str) -> str:
        return "resumo"


def _config(**overrides: object) -> AgentConfig:
    fields: dict[str, object] = {
        "id": "test-agent",
        "nome": "Agente Teste",
        "factory_ia_model": "ollama",
        "model": "llama3.2:latest",
        "descricao": "Descrição do agente",
        "prompt": "Você é o agente de teste.",
    }
    fields.update(overrides)
    return AgentConfig(**fields)  # type: ignore[arg-type]


def _service(
    logger: RecordingLogger,
    models: FakeModelFactory | None = None,
    embedders: FakeEmbedderFactory | None = None,
    *,
    hierarchical: bool = False,
) -> AgentFactoryService:
    embedders = embedders or FakeEmbedderFactory(dimensions=8)
    tree = InMemoryDocumentTreeRepository()
    return AgentFactoryService(
        db_url=DB_URL,
        db_name=DB_NAME,
        logger=logger,
        model_factory=models or FakeModelFactory(),
        embedder_factory=embedders,
        tool_factory=HttpToolFactory(logger=logger),
        tool_repository=InMemoryToolRepository(),
        indexing_service=DocumentIndexingService(
            parser=TextDocumentParser(),
            tree_repository=tree,
            summary_generator=_FixedSummary(),
            embedder_factory=embedders,
            logger=logger,
        )
        if hierarchical
        else None,
        search_factory=KnowledgeSearchFactory(tree_repository=tree, logger=logger) if hierarchical else None,
    )


@pytest.fixture
def docs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A montagem lê ``docs/<doc_name>`` relativo ao cwd."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "manual.md").write_text("# T\n\nIntro.\n\n## A\n\nTexto A.\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return docs


# ── agente sem RAG nem tools ─────────────────────────────────────────


async def test_agente_montado_carrega_identidade_modelo_e_db_da_config() -> None:
    logger = RecordingLogger()
    models = FakeModelFactory()

    agent = await _service(logger, models).create_agent(_config())

    assert (agent.id, agent.name) == ("test-agent", "Agente Teste")
    assert agent.description == "Descrição do agente"
    assert agent.instructions == "Você é o agente de teste."
    assert agent.model is models.models[0]
    assert (agent.model.id, agent.model.provider) == ("llama3.2:latest", "ollama")
    assert isinstance(agent.db, MongoDb)
    assert (agent.db.db_url, agent.db.db_name) == (DB_URL, DB_NAME)
    # sem RAG: nada de knowledge nem das tools de busca que o agno liga com ele
    assert agent.knowledge is None
    assert (agent.search_knowledge, agent.read_chat_history) == (False, False)
    assert not agent.tools
    assert logger.messages("warning", "error") == []
    assert [(r.message, r.context["agent_id"]) for r in logger.records if r.level == "info"] == [
        ("Agente criado", "test-agent")
    ]


@pytest.mark.parametrize(
    ("user_memory", "summary"), [(False, False), (True, False), (False, True), (True, True)]
)
async def test_memoria_e_sumario_do_agente_seguem_a_config(user_memory: bool, summary: bool) -> None:
    agent = await _service(RecordingLogger()).create_agent(
        _config(user_memory_active=user_memory, summary_active=summary)
    )

    # memória de usuário liga as duas formas do agno juntas (automática e pela tool do agente)
    assert (agent.enable_user_memories, agent.enable_agentic_memory) == (user_memory, user_memory)
    assert agent.enable_session_summaries is summary


async def test_config_recusada_pela_fabrica_de_modelo_sobe_como_invalid_model_config_error() -> None:
    """Quem isola o agente é o caso de uso: a fábrica não engole a recusa nem monta agente sem modelo."""
    logger = RecordingLogger()
    service = _service(logger, FakeModelFactory(invalid_models={"llama3.2:latest"}))

    with pytest.raises(InvalidModelConfigError, match="marcado como inválido"):
        await service.create_agent(_config())

    assert logger.messages() == []  # sem log aqui: quem chama loga uma vez


async def test_modelo_recebe_a_model_config_inteira_fora_do_event_loop() -> None:
    """F2-02: campos novos chegam à fábrica; a criação (segredo file:, DNS) roda em thread."""
    models = FakeModelFactory()
    config = _config(
        model_params={"temperature": 0.2}, base_url="https://gw.example.invalid/v1", api_key_ref="env:GW_API_KEY"
    )

    agent = await _service(RecordingLogger(), models).create_agent(config)

    assert models.configs == [config.model_config]
    assert models.on_event_loop == [False]
    assert agent.model is models.models[0]


# ── RAG ──────────────────────────────────────────────────────────────


@pytest.mark.usefixtures("docs_dir")
async def test_rag_semantico_monta_knowledge_com_o_embedder_da_fabrica_e_liga_a_busca(
    offline_knowledge: OfflineKnowledge,
) -> None:
    logger = RecordingLogger()
    embedders = FakeEmbedderFactory(dimensions=8)
    rag = RagConfig(active=True, doc_name="manual.md", model="nomic-embed-text", factory_ia_model="ollama")

    agent = await _service(logger, embedders=embedders).create_agent(_config(rag_config=rag))

    assert isinstance(agent.knowledge, Knowledge)
    vector_db = agent.knowledge.vector_db
    assert vector_db is not None
    assert vector_db.embedder is embedders.embedders[0]  # type: ignore[attr-defined]
    assert embedders.configs == [rag.model_config]
    assert vector_db.collection_name == rag_collection_name("test-agent")  # type: ignore[attr-defined]
    assert (agent.search_knowledge, agent.read_chat_history) == (True, True)
    assert [(i.collection_name, i.path, i.skip_if_exists) for i in offline_knowledge.inserts] == [
        ("rag_test-agent", "docs/manual.md", True)
    ]
    assert logger.messages("warning", "error") == []


@pytest.mark.usefixtures("docs_dir")
@pytest.mark.parametrize(
    ("strategy", "message"),
    [(SearchStrategy.SEMANTIC, "Erro ao criar RAG"), (SearchStrategy.HIERARCHICAL, "Erro ao criar tool hierárquica")],
)
async def test_embedder_recusado_vai_ao_log_com_o_motivo_e_o_agente_sobe_sem_rag(
    strategy: SearchStrategy, message: str
) -> None:
    """``InvalidModelConfigError`` é texto nosso (sem segredo): o operador precisa saber o porquê."""
    logger = RecordingLogger()
    embedders = FakeEmbedderFactory(invalid_models={"emb-recusado"})
    rag = RagConfig(
        active=True, doc_name="manual.md", model="emb-recusado", factory_ia_model="ollama", search_strategy=strategy
    )

    agent = await _service(logger, embedders=embedders, hierarchical=True).create_agent(_config(rag_config=rag))

    assert agent.id == "test-agent"
    assert agent.knowledge is None and agent.search_knowledge is False
    assert not agent.tools  # sem a tool de busca hierárquica
    assert [(r.message, r.context) for r in logger.records if r.level == "warning"] == [
        (
            message,
            {
                "agent_id": "test-agent",
                "error_type": "InvalidModelConfigError",
                "reason": "modelo 'emb-recusado' marcado como inválido no fake",
            },
        )
    ]
