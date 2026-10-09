"""``doc_name`` do RAG confinado a ``docs/`` (F1-07, S3).

``doc_name`` vem do documento do agente no Mongo e virava ``docs/{doc_name}`` sem guarda:
``"../.env"`` indexava o arquivo de segredos no vector store (e o devolvia ao modelo).
Os dois caminhos (semântico e hierárquico) recusam absoluto, ``..`` e symlink para fora,
com log de erro claro e SEM abrir o arquivo (espião em ``open``/``io.open``/``os.open``).
"""

from __future__ import annotations

import builtins
import io
import os
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from agno.knowledge import Knowledge
from agno.knowledge.document import Document
from agno.vectordb.mongodb import MongoDb as MongoVectorDb

from src.application.services.document_indexing_service import DocumentIndexingService
from src.application.services.document_path import DocumentPathError, resolve_document_path
from src.application.services.knowledge_search_factory import KnowledgeSearchFactory
from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.model_config import ModelConfig
from src.domain.entities.rag_config import RagConfig, SearchStrategy
from src.domain.ports.summary_generator_port import ISummaryGenerator
from src.infrastructure.parsers.text_document_parser import TextDocumentParser
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService
from tests.fakes import (
    FakeEmbedderFactory,
    FakeModelFactory,
    InMemoryDocumentTreeRepository,
    InMemoryToolRepository,
    RecordingLogger,
    running_on_event_loop,
)
from tests.fakes.knowledge import OfflineKnowledge

_REAL_KNOWLEDGE_INSERT = Knowledge.insert  # antes de qualquer fixture trocar o insert

pytestmark = pytest.mark.usefixtures("offline_knowledge")

FORA_DE_DOCS = "conteudo-fora-de-docs"


@dataclass
class OpenSpy:
    """Registra cada arquivo aberto (caminho resolvido) e se a abertura foi no event loop."""

    opened: list[tuple[Path, bool]] = field(default_factory=list)

    def paths(self) -> list[Path]:
        return [p for p, _ in self.opened]


@pytest.fixture
def open_spy(monkeypatch: pytest.MonkeyPatch) -> OpenSpy:
    spy = OpenSpy()
    originals = {"builtins": builtins.open, "io": io.open, "os": os.open}

    def remember(file: Any) -> None:
        if isinstance(file, str | bytes | os.PathLike):
            spy.opened.append((Path(os.fsdecode(file)).resolve(), running_on_event_loop()))

    def spy_open(*args: Any, **kwargs: Any) -> Any:
        remember(args[0] if args else kwargs.get("file"))
        return originals["builtins"](*args, **kwargs)

    def spy_os_open(path: Any, *args: Any, **kwargs: Any) -> int:
        remember(path)
        return originals["os"](path, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", spy_open)
    monkeypatch.setattr(io, "open", spy_open)
    monkeypatch.setattr(os, "open", spy_os_open)
    return spy


@dataclass
class Workspace:
    root: Path
    docs: Path
    secret: Path


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Workspace:
    """cwd com ``docs/`` e um arquivo sensível ao lado, alcançável por symlinks de dentro de docs."""
    docs = tmp_path / "docs"
    docs.mkdir()
    (docs / "manual.md").write_text("# Manual\n\nComo usar.\n\n## Login\n\nEntre.\n", encoding="utf-8")
    secret = tmp_path / ".env"
    secret.write_text(FORA_DE_DOCS, encoding="utf-8")
    (docs / "fora.md").symlink_to(secret)  # symlink de arquivo para fora
    (docs / "raiz").symlink_to(tmp_path, target_is_directory=True)  # symlink de diretório para fora
    (docs / "atalho.md").symlink_to(docs / "manual.md")  # symlink que fica dentro de docs
    (docs / "loop1.md").symlink_to(docs / "loop2.md")  # loop de symlink: não resolve
    (docs / "loop2.md").symlink_to(docs / "loop1.md")
    (docs / "pasta").mkdir()  # diretório: o insert do agno percorreria o conteúdo
    (docs / "pasta" / "ok.md").write_text("# Ok\n\nDentro.\n", encoding="utf-8")
    (docs / "vaza").mkdir()  # diretório com symlink para fora (repro do review R1)
    (docs / "vaza" / "link.md").symlink_to(Path("..") / ".." / ".env")
    monkeypatch.chdir(tmp_path)
    return Workspace(root=tmp_path, docs=docs, secret=secret)


_HOSTILE: list[tuple[str, Callable[[Workspace], str]]] = [
    ("dotdot", lambda ws: "../.env"),
    ("dot-dotdot", lambda ws: "./../.env"),
    ("sub-dotdot", lambda ws: "sub/../../.env"),
    ("absoluto", lambda ws: str(ws.secret)),
    ("symlink-arquivo-fora", lambda ws: "fora.md"),
    ("symlink-dir-fora", lambda ws: "raiz/.env"),
    ("so-dotdot", lambda ws: ".."),
    ("o-proprio-docs", lambda ws: "."),
    ("nul", lambda ws: "a\x00b"),
    ("symlink-em-loop", lambda ws: "loop1.md"),
    ("subdir", lambda ws: "pasta"),
    ("subdir-com-barra", lambda ws: "pasta/"),
    ("subdir-com-symlink-para-fora", lambda ws: "vaza"),
    ("nome-longo-errno36", lambda ws: "a" * 300),  # is_dir() levanta OSError (ENAMETOOLONG)
]
HOSTILE = [pytest.param(build, id=name) for name, build in _HOSTILE]
"""``doc_name`` que precisam ser recusados (vazio é "sem documento", tratado à parte)."""


class _FixedSummary(ISummaryGenerator):
    async def generate_summary(self, content: str) -> str:
        return content[:20]


@dataclass
class _Assembly:
    logger: RecordingLogger
    tree: InMemoryDocumentTreeRepository
    service: AgentFactoryService


def _assembly() -> _Assembly:
    logger = RecordingLogger()
    tree = InMemoryDocumentTreeRepository()
    embedder_factory = FakeEmbedderFactory(dimensions=8)
    service = AgentFactoryService(
        db_url="mongodb://mongo.test.invalid:27017",
        db_name="test_db",
        logger=logger,
        model_factory=FakeModelFactory(),
        embedder_factory=embedder_factory,
        tool_factory=None,  # type: ignore[arg-type]  # agente sem tools
        tool_repository=InMemoryToolRepository([]),
        indexing_service=DocumentIndexingService(
            parser=TextDocumentParser(),
            tree_repository=tree,
            summary_generator=_FixedSummary(),
            embedder_factory=embedder_factory,
            logger=logger,
        ),
        search_factory=KnowledgeSearchFactory(tree_repository=tree, logger=logger),
    )
    return _Assembly(logger=logger, tree=tree, service=service)


def _agent(doc_name: str, strategy: SearchStrategy) -> AgentConfig:
    return AgentConfig(
        id="agente-rag",
        nome="Agente RAG",
        factory_ia_model="ollama",
        model="llama3.2:latest",
        descricao="d",
        prompt="p",
        rag_config=RagConfig(
            active=True,
            doc_name=doc_name,
            model="nomic-embed-text:latest",
            factory_ia_model="ollama",
            search_strategy=strategy,
        ),
    )


def _tool_names(agent: Any) -> list[str]:
    return [getattr(t, "name", "") for t in agent.tools or []]


# ── caminho semântico (Knowledge do agno) ───────────────────────────


@pytest.mark.parametrize("build", HOSTILE)
async def test_semantico_recusa_doc_name_fora_de_docs_sem_ler_o_arquivo(
    build: Callable[[Workspace], str], workspace: Workspace, open_spy: OpenSpy, offline_knowledge: OfflineKnowledge
):
    doc_name = build(workspace)
    asm = _assembly()

    agent = await asm.service.create_agent(_agent(doc_name, SearchStrategy.SEMANTIC))

    assert agent.knowledge is None  # RAG desligado para o agente, que sobe
    assert offline_knowledge.inserts == []
    assert workspace.secret.resolve() not in open_spy.paths()
    errors = [r for r in asm.logger.records if r.level == "error"]
    assert [r.message for r in errors] == ["doc_name do RAG rejeitado; RAG do agente desativado"]
    assert errors[0].context["agent_id"] == "agente-rag"
    assert "docs/" in errors[0].context["reason"]


async def test_semantico_doc_name_vazio_e_sem_documento_e_nao_erro(
    workspace: Workspace, offline_knowledge: OfflineKnowledge
):
    asm = _assembly()

    agent = await asm.service.create_agent(_agent("", SearchStrategy.SEMANTIC))

    assert agent.knowledge is not None
    assert offline_knowledge.inserts == []
    assert asm.logger.messages("error") == []
    assert "Nenhum documento especificado para RAG" in asm.logger.messages("info")


@pytest.mark.parametrize(
    ("doc_name", "expected_path"), [("manual.md", "docs/manual.md"), ("atalho.md", "docs/manual.md")]
)
async def test_semantico_aceita_documento_dentro_de_docs(
    doc_name: str, expected_path: str, workspace: Workspace, offline_knowledge: OfflineKnowledge
):
    asm = _assembly()

    agent = await asm.service.create_agent(_agent(doc_name, SearchStrategy.SEMANTIC))

    assert agent.knowledge is not None
    assert [i.path for i in offline_knowledge.inserts] == [expected_path]
    assert asm.logger.messages("warning", "error") == []


# ── caminho hierárquico (leitura própria + indexação) ───────────────


@pytest.mark.parametrize("build", HOSTILE)
async def test_hierarquico_recusa_doc_name_fora_de_docs_sem_ler_o_arquivo(
    build: Callable[[Workspace], str], workspace: Workspace, open_spy: OpenSpy
):
    doc_name = build(workspace)
    asm = _assembly()

    agent = await asm.service.create_agent(_agent(doc_name, SearchStrategy.HIERARCHICAL))

    assert "hierarchical_search" not in _tool_names(agent)
    assert asm.tree._nodes == []  # nada indexado
    assert workspace.secret.resolve() not in open_spy.paths()
    errors = [r for r in asm.logger.records if r.level == "error"]
    assert [r.message for r in errors] == ["doc_name do RAG rejeitado; RAG do agente desativado"]
    assert errors[0].context["agent_id"] == "agente-rag"
    assert "docs/" in errors[0].context["reason"]


async def test_hierarquico_le_documento_dentro_de_docs_fora_do_event_loop(workspace: Workspace, open_spy: OpenSpy):
    asm = _assembly()

    agent = await asm.service.create_agent(_agent("atalho.md", SearchStrategy.HIERARCHICAL))

    assert "hierarchical_search" in _tool_names(agent)
    assert asm.tree._nodes and {n.doc_name for n in asm.tree._nodes} == {"atalho.md"}
    reads = [on_loop for path, on_loop in open_spy.opened if path == (workspace.docs / "manual.md").resolve()]
    assert reads == [False]
    assert asm.logger.messages("warning", "error") == []


async def test_hierarquico_documento_ausente_continua_warning(workspace: Workspace):
    asm = _assembly()

    agent = await asm.service.create_agent(_agent("nao-existe.md", SearchStrategy.HIERARCHICAL))

    assert "hierarchical_search" not in _tool_names(agent)
    assert asm.logger.messages("warning") == ["Documento não encontrado"]
    assert asm.logger.messages("error") == []


# ── resolve_document_path ───────────────────────────────────────────


@pytest.mark.parametrize("build", [*HOSTILE, pytest.param(lambda ws: "", id="vazio")])
def test_resolve_recusa_com_mensagem_clara(build: Callable[[Workspace], str], workspace: Workspace, open_spy: OpenSpy):
    doc_name = build(workspace)

    with pytest.raises(DocumentPathError, match=r"docs/"):
        resolve_document_path(doc_name)
    assert open_spy.opened == []


@pytest.mark.parametrize("value", [None, 123, b"manual.md"])
def test_resolve_recusa_tipo_que_nao_e_str(value: Any, workspace: Workspace):
    with pytest.raises(DocumentPathError, match="docs/"):
        resolve_document_path(value)


@pytest.mark.parametrize(
    ("doc_name", "expected"),
    [
        ("manual.md", Path("docs/manual.md")),
        ("atalho.md", Path("docs/manual.md")),  # symlink resolvido, dentro de docs
        ("nao-existe.md", Path("docs/nao-existe.md")),  # ausência é tratada por quem lê
    ],
)
def test_resolve_devolve_caminho_relativo_dentro_de_docs(doc_name: str, expected: Path, workspace: Workspace):
    assert resolve_document_path(doc_name) == expected


def test_resolve_aceita_subdiretorio_real_de_docs(workspace: Workspace):
    (workspace.docs / "manuais").mkdir()
    (workspace.docs / "manuais" / "a.txt").write_text("x", encoding="utf-8")

    assert resolve_document_path("manuais/a.txt") == Path("docs/manuais/a.txt")


def test_resolve_recusa_arquivo_que_nao_e_regular(workspace: Workspace, open_spy: OpenSpy):
    """FIFO (ou device) em docs/: ler travaria a thread para sempre."""
    os.mkfifo(workspace.docs / "fila.md")

    with pytest.raises(DocumentPathError, match="arquivo regular"):
        resolve_document_path("fila.md")
    assert open_spy.opened == []


def test_symlink_do_diretorio_vaza_aponta_mesmo_para_o_segredo(workspace: Workspace):
    """Controle: o caso ``subdir-com-symlink-para-fora`` alcança o arquivo sensível se for percorrido."""
    assert (workspace.docs / "vaza" / "link.md").resolve() == workspace.secret.resolve()


# ── insert REAL do agno (lê, divide e embeda): o que chegaria ao vector store ──


@pytest.fixture
def stored_chunks(monkeypatch: pytest.MonkeyPatch, offline_knowledge: OfflineKnowledge) -> list[str]:
    """Volta o ``Knowledge.insert`` real; o vector db só guarda o texto dos chunks (sem Mongo)."""
    chunks: list[str] = []

    def store(self: MongoVectorDb, content_hash: str, documents: list[Document], filters: Any = None) -> None:
        chunks.extend(d.content for d in documents)

    monkeypatch.setattr(Knowledge, "insert", _REAL_KNOWLEDGE_INSERT)
    monkeypatch.setattr(MongoVectorDb, "content_hash_exists", lambda self, content_hash: False)
    monkeypatch.setattr(MongoVectorDb, "insert", store)
    monkeypatch.setattr(MongoVectorDb, "upsert", store)
    return chunks


async def test_controle_insert_real_do_agno_em_diretorio_segue_symlink_para_fora(
    workspace: Workspace, stored_chunks: list[str]
):
    """Controle do review R1: ``Knowledge.insert(path=<dir>)`` percorre o diretório e embeda o segredo."""
    knowledge = Knowledge(
        vector_db=MongoVectorDb(
            collection_name="rag_controle",
            db_url="mongodb://mongo.test.invalid:27017",
            database="test_db",
            embedder=FakeEmbedderFactory(dimensions=8).create_embedder(ModelConfig("fake", "e")),
        )
    )

    knowledge.insert(path="docs/vaza", skip_if_exists=True)

    assert any(FORA_DE_DOCS in c for c in stored_chunks)


@pytest.mark.parametrize("doc_name", ["vaza", "vaza/", "pasta"])
async def test_semantico_com_insert_real_nao_embeda_nada_de_diretorio(
    doc_name: str, workspace: Workspace, stored_chunks: list[str]
):
    asm = _assembly()

    agent = await asm.service.create_agent(_agent(doc_name, SearchStrategy.SEMANTIC))

    assert agent.knowledge is None
    assert stored_chunks == []
    assert asm.logger.messages("error") == ["doc_name do RAG rejeitado; RAG do agente desativado"]


async def test_semantico_com_insert_real_embeda_o_documento_valido(workspace: Workspace, stored_chunks: list[str]):
    asm = _assembly()

    agent = await asm.service.create_agent(_agent("manual.md", SearchStrategy.SEMANTIC))

    assert agent.knowledge is not None
    assert any("Como usar" in c for c in stored_chunks)
    assert all(FORA_DE_DOCS not in c for c in stored_chunks)


@pytest.fixture
def locked_dir(workspace: Workspace) -> Iterator[Path]:
    """Subdiretório de docs/ sem permissão (chmod 000); root ignora permissão, então pula."""
    if os.geteuid() == 0:
        pytest.skip("root ignora chmod 000")
    locked = workspace.docs / "trancada"
    locked.mkdir()
    (locked / "x.md").write_text("x", encoding="utf-8")
    locked.chmod(0)
    try:
        yield locked
    finally:
        locked.chmod(0o755)  # deixa o tmp_path removível


@pytest.mark.usefixtures("locked_dir")
def test_resolve_converte_erro_de_permissao_em_document_path_error():
    with pytest.raises(DocumentPathError, match="não pôde ser resolvido"):
        resolve_document_path("trancada/x.md")


@pytest.mark.usefixtures("locked_dir")
@pytest.mark.parametrize("strategy", [SearchStrategy.SEMANTIC, SearchStrategy.HIERARCHICAL])
async def test_subdiretorio_sem_permissao_nao_descarta_o_agente(strategy: SearchStrategy):
    asm = _assembly()

    agent = await asm.service.create_agent(_agent("trancada/x.md", strategy))

    assert agent.id == "agente-rag"
    assert agent.knowledge is None and "hierarchical_search" not in _tool_names(agent)
    assert asm.logger.messages("error") == ["doc_name do RAG rejeitado; RAG do agente desativado"]
