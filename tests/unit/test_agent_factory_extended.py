"""``AgentFactoryService``: caminhos de erro do RAG semântico, com o ``Agent``/``Knowledge`` reais do agno.

Sem ``patch`` no módulo da fábrica: o I/O do agno é cortado na borda por ``cut_agno_io`` (o mesmo
corte da fixture ``offline_knowledge``), e a falha do ``Knowledge.insert`` é injetada pelo gancho dele.
Em todos os casos o agente sobe; o que muda é se ele fica com ``knowledge`` e o que vai ao log.

Coberto em outro lugar (e por isso não repetido): falha ao buscar/criar tools
(``test_agent_factory_tools.py``), ``doc_name`` vazio ou rejeitado (``tests/security/test_rag_doc_path.py``).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from agno.knowledge import Knowledge

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.rag_config import RagConfig
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService
from src.infrastructure.runtime.agno.http_tool_factory import HttpToolFactory
from tests.fakes import FakeEmbedderFactory, FakeModelFactory, InMemoryToolRepository, RecordingLogger
from tests.fakes.knowledge import cut_agno_io

SECRET = "mongodb://app:s3nh4@db.interno.invalid:27017"  # noqa: S105 - marcador de vazamento, não é segredo


def _config(rag: RagConfig) -> AgentConfig:
    return AgentConfig(
        id="test-agent",
        nome="Agente Teste",
        factory_ia_model="ollama",
        model="llama3.2:latest",
        descricao="desc",
        prompt="prompt",
        rag_config=rag,
    )


def _service(logger: RecordingLogger, embedders: FakeEmbedderFactory) -> AgentFactoryService:
    return AgentFactoryService(
        db_url="mongodb://mongo.test.invalid:27017",
        db_name="test_db",
        logger=logger,
        model_factory=FakeModelFactory(),
        embedder_factory=embedders,
        tool_factory=HttpToolFactory(logger=logger),
        tool_repository=InMemoryToolRepository(),
    )


@pytest.fixture
def docs_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A montagem lê ``docs/<doc_name>`` relativo ao cwd."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "manual.md").write_text("# Manual\n\nTexto.\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return docs


class _InsertFailure:
    """Gancho do ``Knowledge.insert``: levanta ``error`` (ou só conta, se não houver)."""

    def __init__(self) -> None:
        self.error: Exception | None = None
        self.paths: list[str | None] = []

    def __call__(self, knowledge: Knowledge, args: tuple[Any, ...], kwargs: dict[str, Any]) -> None:
        self.paths.append(kwargs.get("path"))
        if self.error is not None:
            raise self.error


@pytest.fixture
def insert(monkeypatch: pytest.MonkeyPatch) -> Iterator[_InsertFailure]:
    hook = _InsertFailure()
    with cut_agno_io(monkeypatch, on_insert=hook):
        yield hook


# ── RAG ativo sem modelo de embedder ─────────────────────────────────


@pytest.mark.parametrize(
    "rag",
    [
        RagConfig(active=True, model="", factory_ia_model=""),
        RagConfig(active=True, model="embed-v1", factory_ia_model=""),
        RagConfig(active=True, model="", factory_ia_model="ollama"),
    ],
    ids=["sem-os-dois", "sem-provider", "sem-modelo"],
)
async def test_rag_ativo_sem_provider_ou_modelo_e_ignorado_com_aviso(rag: RagConfig, insert: _InsertFailure) -> None:
    logger = RecordingLogger()
    embedders = FakeEmbedderFactory()

    agent = await _service(logger, embedders).create_agent(_config(rag))

    assert agent.knowledge is None
    assert (agent.search_knowledge, agent.read_chat_history) == (False, False)
    assert embedders.created == [] and insert.paths == []  # nem embedder nem documento
    assert logger.messages("warning", "error") == ["RAG ativo sem factory_ia_model ou model — ignorando"]


# ── falha ao indexar o documento ─────────────────────────────────────


@pytest.mark.usefixtures("docs_dir")
async def test_documento_que_o_agno_nao_acha_vira_aviso_e_o_agente_fica_com_o_knowledge_vazio(
    insert: _InsertFailure,
) -> None:
    logger = RecordingLogger()
    insert.error = FileNotFoundError("docs/manual.md")
    rag = RagConfig(active=True, model="m", factory_ia_model="ollama", doc_name="manual.md")

    agent = await _service(logger, FakeEmbedderFactory()).create_agent(_config(rag))

    assert isinstance(agent.knowledge, Knowledge)
    assert (agent.search_knowledge, agent.read_chat_history) == (True, True)
    assert insert.paths == ["docs/manual.md"]
    assert [(r.level, r.message, r.context) for r in logger.records if r.level in ("warning", "error")] == [
        ("warning", "Documento não encontrado", {"path": "docs/manual.md"})
    ]


@pytest.mark.usefixtures("docs_dir")
async def test_falha_ao_indexar_o_documento_loga_so_o_tipo_e_o_agente_sobe_com_o_knowledge(
    insert: _InsertFailure,
) -> None:
    """O texto do erro do driver (URI com credencial) não vai ao log."""
    logger = RecordingLogger()
    insert.error = RuntimeError(f"falha ao gravar em {SECRET}")
    rag = RagConfig(active=True, model="m", factory_ia_model="ollama", doc_name="manual.md")

    agent = await _service(logger, FakeEmbedderFactory()).create_agent(_config(rag))

    assert agent.id == "test-agent"
    assert isinstance(agent.knowledge, Knowledge)
    assert [(r.level, r.message, r.context) for r in logger.records if r.level in ("warning", "error")] == [
        ("error", "Erro ao carregar documento RAG", {"path": "docs/manual.md", "error_type": "RuntimeError"})
    ]
    assert SECRET not in repr(logger.records)
