"""Coleção vetorial do RAG semântico por agente (F1-07, B6).

Antes, todo agente gravava e buscava na coleção ``rag``: um agente via os chunks do outro e
embedders com dimensões diferentes colidiam no mesmo índice. Sem Mongo real: o I/O é
cortado na borda do agno (fixture ``offline_knowledge``); a integração com Atlas é ``live``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.rag_config import RagConfig, SearchStrategy
from src.infrastructure.runtime.agno.agent_factory_service import (
    AgentFactoryService,
    rag_collection_name,
)
from tests.fakes import (
    FakeEmbedderFactory,
    FakeModelFactory,
    InMemoryToolRepository,
    RecordingLogger,
)
from tests.fakes.knowledge import OfflineKnowledge

pytestmark = pytest.mark.usefixtures("offline_knowledge")

# Nome de coleção que o MongoDB aceita sem escape: sem "$", NUL, "system." nem ".";
# curto o bastante para o limite de namespace (db.coleção ≤ 255 bytes).
_SAFE_COLLECTION = re.compile(r"rag_[A-Za-z0-9_-]{1,80}")


def _service(logger: RecordingLogger) -> AgentFactoryService:
    return AgentFactoryService(
        db_url="mongodb://mongo.test.invalid:27017",
        db_name="test_db",
        logger=logger,
        model_factory=FakeModelFactory(),
        embedder_factory=FakeEmbedderFactory(dimensions=8),
        tool_factory=None,  # type: ignore[arg-type]  # agente sem tools
        tool_repository=InMemoryToolRepository([]),
    )


def _rag_agent(agent_id: str, doc_name: str, embed_model: str = "nomic-embed-text:latest") -> AgentConfig:
    return AgentConfig(
        id=agent_id,
        nome=f"Agente {agent_id}",
        factory_ia_model="ollama",
        model="llama3.2:latest",
        descricao="d",
        prompt="p",
        rag_config=RagConfig(
            active=True,
            doc_name=doc_name,
            model=embed_model,
            factory_ia_model="ollama",
            search_strategy=SearchStrategy.SEMANTIC,
        ),
    )


@pytest.fixture
def docs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A montagem lê ``docs/<doc_name>`` relativo ao cwd."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "financeiro.md").write_text("# Financeiro\n\nSegredo do time A.\n", encoding="utf-8")
    (docs / "suporte.md").write_text("# Suporte\n\nManual do time B.\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return docs


@pytest.mark.usefixtures("docs_dir")
async def test_dois_agentes_com_documentos_diferentes_usam_colecoes_diferentes(offline_knowledge: OfflineKnowledge):
    logger = RecordingLogger()
    service = _service(logger)

    agent_a = await service.create_agent(_rag_agent("time-a", "financeiro.md"))
    agent_b = await service.create_agent(_rag_agent("time-b", "suporte.md", embed_model="mxbai-embed-large"))

    assert agent_a.knowledge is not None and agent_b.knowledge is not None
    assert agent_a.knowledge.vector_db.collection_name == "rag_time-a"
    assert agent_b.knowledge.vector_db.collection_name == "rag_time-b"
    assert [(i.collection_name, i.path, i.skip_if_exists) for i in offline_knowledge.inserts] == [
        ("rag_time-a", "docs/financeiro.md", True),
        ("rag_time-b", "docs/suporte.md", True),
    ]
    assert logger.messages("warning", "error") == []


@pytest.mark.usefixtures("docs_dir")
async def test_nenhum_agente_usa_a_colecao_legada_compartilhada(offline_knowledge: OfflineKnowledge):
    service = _service(RecordingLogger())

    agent = await service.create_agent(_rag_agent("qualquer", "suporte.md"))

    assert agent.knowledge is not None
    assert agent.knowledge.vector_db.collection_name != "rag"
    assert {i.collection_name for i in offline_knowledge.inserts} == {"rag_qualquer"}


@pytest.mark.usefixtures("docs_dir")
async def test_montagem_do_knowledge_e_insert_rodam_fora_do_event_loop(offline_knowledge: OfflineKnowledge):
    """``Knowledge()`` consulta o Mongo (exists/create) e ``insert`` lê, embeda e grava: tudo síncrono."""
    service = _service(RecordingLogger())

    await service.create_agent(_rag_agent("time-a", "financeiro.md"))

    assert offline_knowledge.exists_on_event_loop == [False]
    assert [i.on_event_loop for i in offline_knowledge.inserts] == [False]


# ── nome da coleção ─────────────────────────────────────────────────


@pytest.mark.parametrize("agent_id", ["time-a", "rag-semantic", "agente01", "a"])
def test_id_simples_vira_rag_mais_o_id(agent_id: str):
    assert rag_collection_name(agent_id) == f"rag_{agent_id}"


@pytest.mark.parametrize(
    "agent_id",
    ["Time A", "time.a", "time_a", "TIME-A", "a$b", "system.users", "ação", "a\x00b", "x" * 300, "../x", "a/b"],
)
def test_id_fora_do_padrao_vira_nome_valido_com_hash(agent_id: str):
    name = rag_collection_name(agent_id)

    assert _SAFE_COLLECTION.fullmatch(name), name
    assert re.fullmatch(r"rag_[a-z0-9-]+_[0-9a-f]{12}", name), name
    assert name == rag_collection_name(agent_id)  # determinístico: reinício acha a mesma coleção


def test_ids_diferentes_nunca_colidem_mesmo_com_o_mesmo_slug():
    ids = ["time-a", "time_a", "Time-A", "TIME-A", "time.a", "time a", "time--a", "time-a "]

    names = [rag_collection_name(i) for i in ids]

    assert len(set(names)) == len(ids), dict(zip(ids, names, strict=True))


def test_id_simples_igual_ao_nome_com_hash_de_outro_id_nao_colide():
    """Forma com hash tem dois ``_``; id simples não tem ``_``: os conjuntos são disjuntos."""
    hashed = rag_collection_name("time.a")  # rag_time-a_<hash>
    crafted_id = hashed.removeprefix("rag_")  # contém "_": não é id simples

    assert rag_collection_name(crafted_id) != hashed
