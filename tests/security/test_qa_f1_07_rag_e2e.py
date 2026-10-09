"""QA do F1-07: RAG de ponta a ponta com o app real (AppFactory + AgentOS + chave run).

Complementa os testes do dev (``test_rag_doc_path.py``, ``test_rag_collection_per_agent.py``):
agentes com RAG semântico e hierárquico no MESMO app, cada um buscando só no próprio conteúdo
(o modelo roteirizado chama a tool de busca e o resultado volta a ele); ``doc_name`` hostil
num agente sem derrubar os outros; subdiretório válido; mesmo arquivo em dois agentes; o event
loop não é bloqueado na montagem nem na busca; resumo com modelo que falha vira fallback com
log e sem stack ao cliente.

Sem Mongo: o ``Knowledge`` do agno é cortado na borda (``offline_knowledge``) e o vector db é um
dicionário por nome de coleção (``FakeVectorStore``), então "coleção diferente" é observável
pelo que a busca devolve ao modelo. As chaves são valores de teste, não segredos.
"""

from __future__ import annotations

import asyncio
import json
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import pytest
from agno.agent import Agent
from agno.db.in_memory import InMemoryDb
from agno.knowledge import Knowledge
from agno.knowledge.document import Document
from agno.models.response import ModelResponse
from agno.vectordb.mongodb import MongoDb as MongoVectorDb
from starlette.testclient import TestClient

from src.application.services.document_indexing_service import DocumentIndexingService
from src.application.services.knowledge_search_factory import KnowledgeSearchFactory
from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase
from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.model_config import ModelConfig
from src.domain.entities.rag_config import RagConfig, SearchStrategy
from src.infrastructure.parsers.text_document_parser import TextDocumentParser
from src.infrastructure.runtime.agno import AgnoRuntime, agent_factory_service
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService, rag_collection_name
from src.infrastructure.runtime.agno.team_factory_service import TeamFactoryService
from src.infrastructure.services.llm_summary_generator import LLMSummaryGenerator
from src.infrastructure.web.app_factory import AppFactory
from tests.fakes import (
    FakeChatModel,
    FakeEmbedder,
    FakeEmbedderFactory,
    FakeModelCall,
    FakeModelFactory,
    InMemoryAgentConfigRepository,
    InMemoryDocumentTreeRepository,
    InMemoryToolRepository,
    RecordingLogger,
)
from tests.fakes.knowledge import OfflineKnowledge
from tests.fakes.models import factory_call

pytestmark = pytest.mark.usefixtures("offline_knowledge")

RUN_KEY = "qa7-run-key-" + "r" * 22
ADMIN_KEY = "qa7-admin-key-" + "a" * 20
ALL_INTERFACES = "0.0.0" + ".0"
RUN = {"Authorization": f"Bearer {RUN_KEY}"}
_ENV_NAMES = ("ENVIRONMENT", "ENABLE_DOCS", "CORS_ALLOWED_ORIGINS", "APP_HOST", "API_KEY_RUN", "API_KEY_ADMIN")

FINANCEIRO = "MARCA-FINANCEIRO-SEMANTICO"
SUPORTE = "MARCA-SUPORTE-SEMANTICO"
HIER_A = "MARCA-HIER-A-REEMBOLSO"
HIER_B = "MARCA-HIER-B-SENHA"
GUIA = "MARCA-GUIA-SUBDIR"
SEGREDO = "SEGREDO-DO-DOTENV"
ALL_MARKS = (FINANCEIRO, SUPORTE, HIER_A, HIER_B, GUIA)

DOCS = {
    "financeiro.md": f"# Financeiro\n\n{FINANCEIRO} reembolso e fatura.\n",
    "suporte.md": f"# Suporte\n\n{SUPORTE} senha e login.\n",
    "manuais/guia.md": f"# Guia\n\n{GUIA} guia no subdiretório.\n",
    "hier-a.md": (
        f"# Financeiro\n\nIntrodução do financeiro.\n\n## Reembolso\n\n{HIER_A} envie a nota do reembolso.\n\n"
        "## Fatura\n\nA fatura vence todo dia 5, pague a fatura em dia.\n"
    ),
    "hier-b.md": (
        f"# Acesso\n\nIntrodução do acesso.\n\n## Senha\n\n{HIER_B} troque a senha no portal.\n\n"
        "## VPN\n\nA vpn exige token da vpn.\n"
    ),
}


# ── ambiente: docs/ em tmp, db em memória, vector db por coleção ──────


@pytest.fixture
def workspace(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    for name, text in DOCS.items():
        path = tmp_path / "docs" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")
    (tmp_path / ".env").write_text(f"API_KEY={SEGREDO}\n", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.fixture(autouse=True)
def memory_db(monkeypatch: pytest.MonkeyPatch) -> InMemoryDb:
    db = InMemoryDb()
    monkeypatch.setattr(agent_factory_service, "MongoAgentDb", lambda **_: db)
    return db


@dataclass
class FakeVectorStore:
    """Vector db por nome de coleção (o que o Mongo faria): ``insert`` grava, ``search`` lê só a própria."""

    collections: dict[str, list[str]] = field(default_factory=dict)
    on_blocking_call: Callable[[], None] | None = None
    search_collections: list[str] = field(default_factory=list)

    def install(self, mp: pytest.MonkeyPatch) -> None:
        store = self

        def insert(kb: Knowledge, *args: Any, **kwargs: Any) -> None:
            if store.on_blocking_call:
                store.on_blocking_call()
            name = str(getattr(kb.vector_db, "collection_name"))  # noqa: B009
            text = Path(kwargs["path"]).read_text(encoding="utf-8")
            store.collections.setdefault(name, []).extend(p for p in text.split("\n\n") if p.strip())

        def exists(db: MongoVectorDb) -> bool:
            if store.on_blocking_call:
                store.on_blocking_call()
            return True

        def search(db: MongoVectorDb, query: str, limit: int = 5, *args: Any, **kwargs: Any) -> list[Document]:
            name = str(db.collection_name)
            store.search_collections.append(name)
            return [Document(content=c, name=name) for c in store.collections.get(name, [])][:limit]

        async def async_search(
            db: MongoVectorDb, query: str, limit: int = 5, *args: Any, **kwargs: Any
        ) -> list[Document]:
            return search(db, query, limit)

        mp.setattr(Knowledge, "insert", insert)
        mp.setattr(MongoVectorDb, "exists", exists)
        mp.setattr(MongoVectorDb, "search", search)
        mp.setattr(MongoVectorDb, "async_search", async_search)


@pytest.fixture
def vector_store(monkeypatch: pytest.MonkeyPatch, offline_knowledge: OfflineKnowledge) -> FakeVectorStore:
    store = FakeVectorStore()
    store.install(monkeypatch)  # depois do offline_knowledge: este insert/search substitui o dele
    return store


_KEYWORDS = ("reembolso", "fatura", "senha", "vpn", "login")


class KeywordEmbedder(FakeEmbedder):
    """Um eixo por palavra-chave (+ um eixo "outros"): relevância previsível sem modelo real."""

    def get_embedding(self, text: str) -> list[float]:
        self.calls.append(text)
        counts = [float(text.lower().count(k)) for k in _KEYWORDS]
        return [*counts, 0.0 if any(counts) else 1.0]


class KeywordEmbedderFactory(FakeEmbedderFactory):
    """Guarda em ``created``/``embedders`` (da base) a instância realmente devolvida."""

    def __init__(self, *, wait_for_loop: Callable[[], None] | None = None) -> None:
        super().__init__()
        self._wait_for_loop = wait_for_loop

    def create_embedder(self, config: ModelConfig) -> KeywordEmbedder:
        self.created.append(factory_call(config))
        factory_ia_model, model_id = config.provider, config.model_id
        wait = self._wait_for_loop

        class _Embedder(KeywordEmbedder):
            def get_embedding(self, text: str) -> list[float]:
                if wait:
                    wait()
                return super().get_embedding(text)

        embedder = _Embedder(id=model_id, provider=factory_ia_model, dimensions=len(_KEYWORDS) + 1)
        self.embedders.append(embedder)
        return embedder


@dataclass
class Assembly:
    service: AgentFactoryService
    logger: RecordingLogger
    tree: InMemoryDocumentTreeRepository
    embedders: KeywordEmbedderFactory
    summary_models: FakeModelFactory


def _assembly(
    *,
    summary_models: FakeModelFactory | None = None,
    embedders: KeywordEmbedderFactory | None = None,
    logger: RecordingLogger | None = None,
) -> Assembly:
    logger = logger or RecordingLogger()
    tree = InMemoryDocumentTreeRepository()
    embedders = embedders or KeywordEmbedderFactory()
    summary_models = summary_models or FakeModelFactory(responses=["resumo-do-modelo"] * 50)
    service = AgentFactoryService(
        db_url="mongodb://mongo.test.invalid:27017",
        db_name="test_db",
        logger=logger,
        model_factory=FakeModelFactory(responses=["resposta"] * 30),
        embedder_factory=embedders,
        tool_factory=None,  # type: ignore[arg-type]  # agentes sem tools
        tool_repository=InMemoryToolRepository([]),
        indexing_service=DocumentIndexingService(
            parser=TextDocumentParser(),
            tree_repository=tree,
            summary_generator=LLMSummaryGenerator(
                model_factory=summary_models, factory_ia_model="fake", model_id="resumo", logger=logger
            ),
            embedder_factory=embedders,
            logger=logger,
        ),
        search_factory=KnowledgeSearchFactory(tree_repository=tree, logger=logger),
    )
    return Assembly(service, logger, tree, embedders, summary_models)


def _config(agent_id: str, doc_name: Any, strategy: SearchStrategy) -> AgentConfig:
    return AgentConfig(
        id=agent_id,
        nome=f"Agente {agent_id}",
        factory_ia_model="fake",
        model="modelo",
        descricao="d",
        prompt="Você responde com base no conhecimento.",
        rag_config=RagConfig(
            active=True,
            doc_name=doc_name,
            model="embed",
            factory_ia_model="fake",
            search_strategy=strategy,
        ),
    )


SEM = SearchStrategy.SEMANTIC
HIER = SearchStrategy.HIERARCHICAL


@dataclass
class SearchBrain(FakeChatModel):
    """Chama a tool de busca do agente (pelo nome oferecido) e, com o resultado, responde.

    O roteiro é por papel (tools oferecidas / resultado já presente), não por posição.
    """

    query: str = "reembolso"

    def _next_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        call: FakeModelCall = self._as_call(args, kwargs)
        self.calls.append(call)
        if any(role == "tool" for role, _ in call.messages):
            return ModelResponse(role="assistant", content="ok, li o resultado da busca")
        for name in ("search_knowledge_base", "search_knowledge"):
            if name in call.tool_names:
                return ModelResponse(
                    role="assistant",
                    tool_calls=[
                        {
                            "id": "c1",
                            "type": "function",
                            "function": {"name": name, "arguments": json.dumps({"query": self.query})},
                        }
                    ],
                )
        return ModelResponse(role="assistant", content="sem tool de busca")


def _tool_results(model: FakeChatModel) -> str:
    """Tudo que o modelo recebeu como resultado de tool (o que a busca devolveu a ele)."""
    return "\n".join(content for call in model.calls for role, content in call.messages if role == "tool")


def _offered_tools(model: FakeChatModel) -> set[str]:
    return {name for call in model.calls for name in call.tool_names}


def _search_entrypoint(agent: Agent) -> Callable[..., Any]:
    """Função async ``search_knowledge`` do Toolkit hierárquico do agente."""
    toolkit = (agent.tools or [])[0]
    return toolkit.async_functions["search_knowledge"].entrypoint  # type: ignore[union-attr,return-value]


def _attach(agent: Agent, query: str = "reembolso") -> SearchBrain:
    brain = SearchBrain(query=query)
    agent.model = brain
    return brain


@pytest.fixture
def client_for(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[[list[Agent]], TestClient]]:
    clients: list[TestClient] = []

    def _build(agents: list[Agent]) -> TestClient:
        for name in _ENV_NAMES:
            monkeypatch.delenv(name, raising=False)
        monkeypatch.setenv("AGNO_TELEMETRY", "false")
        monkeypatch.setenv("APP_HOST", ALL_INTERFACES)
        monkeypatch.setenv("ENVIRONMENT", "production")
        monkeypatch.setenv("API_KEY_RUN", RUN_KEY)
        monkeypatch.setenv("API_KEY_ADMIN", ADMIN_KEY)
        factory = AppFactory()
        app = factory.create_app()
        factory._mount_agent_os(app, agents, [])
        client = TestClient(app, raise_server_exceptions=False)
        clients.append(client)
        return client

    yield _build
    for client in clients:
        client.close()


def _run(client: TestClient, agent_id: str) -> dict[str, Any]:
    response = client.post(f"/agents/{agent_id}/runs", data={"message": "pergunta", "stream": "false"}, headers=RUN)
    assert response.status_code == 200, response.text
    body: dict[str, Any] = response.json()
    return body


# ── semântico + hierárquico no mesmo app: cada um busca só no próprio conteúdo ──


@pytest.mark.usefixtures("workspace")
async def test_agentes_semantico_e_hierarquico_no_mesmo_app_buscam_so_no_proprio_conteudo(
    vector_store: FakeVectorStore, client_for: Callable[[list[Agent]], TestClient]
):
    asm = _assembly()
    sem_a = await asm.service.create_agent(_config("fin", "financeiro.md", SEM))
    sem_b = await asm.service.create_agent(_config("sup", "suporte.md", SEM))
    hier_a = await asm.service.create_agent(_config("hier-a", "hier-a.md", HIER))
    hier_b = await asm.service.create_agent(_config("hier-b", "hier-b.md", HIER))
    brains = {
        "fin": _attach(sem_a),
        "sup": _attach(sem_b),
        "hier-a": _attach(hier_a, "reembolso"),
        "hier-b": _attach(hier_b, "senha"),
    }
    client = client_for([sem_a, sem_b, hier_a, hier_b])

    for agent_id in brains:
        body = _run(client, agent_id)
        assert body["status"] == "COMPLETED", body
        assert body["content"] == "ok, li o resultado da busca"

    own = {"fin": FINANCEIRO, "sup": SUPORTE, "hier-a": HIER_A, "hier-b": HIER_B}
    for agent_id, brain in brains.items():
        results = _tool_results(brain)
        assert own[agent_id] in results, (agent_id, results)
        for mark in ALL_MARKS:
            if mark != own[agent_id]:
                assert mark not in results, (agent_id, mark, results)
    assert "search_knowledge_base" in _offered_tools(brains["fin"])
    assert "search_knowledge" in _offered_tools(brains["hier-a"])
    assert set(vector_store.collections) == {"rag_fin", "rag_sup"}
    assert sorted(set(vector_store.search_collections)) == ["rag_fin", "rag_sup"]


@pytest.mark.usefixtures("workspace")
async def test_hierarquico_aplica_limiar_na_busca_do_agente_montado():
    """Pergunta sobre reembolso devolve a seção de reembolso e não a de fatura (score abaixo do limiar)."""
    asm = _assembly()
    agent = await asm.service.create_agent(_config("hier-a", "hier-a.md", HIER))
    brain = _attach(agent, "reembolso")

    entrypoint = _search_entrypoint(agent)
    text = await entrypoint(query="reembolso")

    assert HIER_A in text
    assert "fatura vence" not in text
    assert brain.calls == []


@pytest.mark.usefixtures("workspace")
async def test_controle_coleção_compartilhada_faria_um_agente_ver_o_conteudo_do_outro(
    vector_store: FakeVectorStore, monkeypatch: pytest.MonkeyPatch
):
    """Controle negativo: com a coleção ``rag`` única (B6), a mesma montagem vaza conteúdo entre agentes."""
    monkeypatch.setattr(agent_factory_service, "rag_collection_name", lambda agent_id: "rag")
    asm = _assembly()
    a = await asm.service.create_agent(_config("fin", "financeiro.md", SEM))
    await asm.service.create_agent(_config("sup", "suporte.md", SEM))
    brain = _attach(a)

    assert rag_collection_name("fin") == "rag_fin"  # a função real segue correta; só o gancho foi trocado
    docs = await a.knowledge.asearch("qualquer")  # type: ignore[union-attr]
    contents = " ".join(d.content for d in docs)
    assert FINANCEIRO in contents and SUPORTE in contents
    assert brain.calls == []


# ── doc_name hostil num agente não derruba os outros nem o startup ───


@pytest.mark.usefixtures("workspace")
async def test_doc_name_hostil_num_agente_nao_derruba_os_outros_nem_o_startup(
    vector_store: FakeVectorStore, client_for: Callable[[list[Agent]], TestClient], workspace: Path
):
    asm = _assembly()
    configs = [
        _config("ruim-sem", "../.env", SEM),
        _config("bom-sem", "financeiro.md", SEM),
        _config("ruim-hier", "../.env", HIER),
        _config("bom-hier", "hier-a.md", HIER),
        _config("abs-sem", str(workspace / ".env"), SEM),
        _config("tipo-ruim", 123, HIER),
    ]
    runtime = AgnoRuntime(
        agent_factory=asm.service,
        team_factory=TeamFactoryService(
            db_url="mongodb://mongo.test.invalid:27017", logger=asm.logger, model_factory=FakeModelFactory()
        ),
    )
    use_case = GetActiveAgentsUseCase(runtime, InMemoryAgentConfigRepository(configs), asm.logger)

    agents = await use_case.execute()  # gather: um create_agent que levantasse sumiria da lista

    assert sorted(a.id or "" for a in agents) == sorted(c.id for c in configs)
    by_id = {a.id: a for a in agents}
    assert by_id["ruim-sem"].knowledge is None and by_id["abs-sem"].knowledge is None
    assert by_id["bom-sem"].knowledge is not None
    rejected = [r for r in asm.logger.records if r.message == "doc_name do RAG rejeitado; RAG do agente desativado"]
    assert sorted(r.context["agent_id"] for r in rejected) == ["abs-sem", "ruim-hier", "ruim-sem", "tipo-ruim"]
    assert all(r.level == "error" for r in rejected)

    brains = {agent_id: _attach(agent) for agent_id, agent in by_id.items() if agent_id}
    client = client_for(agents)  # o app sobe com todos
    listed = client.get("/agents", headers=RUN)
    assert listed.status_code == 200
    assert sorted(a["id"] for a in listed.json()) == sorted(c.id for c in configs)
    for agent_id in brains:
        assert _run(client, agent_id)["status"] == "COMPLETED"

    assert FINANCEIRO in _tool_results(brains["bom-sem"])
    assert HIER_A in _tool_results(brains["bom-hier"])
    everything = "".join(_tool_results(b) for b in brains.values())
    assert SEGREDO not in everything
    assert all(SEGREDO not in "".join(chunks) for chunks in vector_store.collections.values())
    assert all(n.doc_name != "../.env" and SEGREDO not in n.content for n in asm.tree._nodes)
    assert _offered_tools(brains["ruim-sem"]).isdisjoint({"search_knowledge_base", "search_knowledge"})
    assert _offered_tools(brains["ruim-hier"]).isdisjoint({"search_knowledge_base", "search_knowledge"})


@pytest.mark.usefixtures("workspace")
@pytest.mark.parametrize("doc_name", ["..\\.env", "%2e%2e/.env", "..%2f.env", "\uff0e\uff0e/.env", " ../.env"])
async def test_doc_name_com_codificacao_ou_barra_invertida_nao_le_fora_de_docs(
    doc_name: str, vector_store: FakeVectorStore
):
    """No Linux ``..\\.env`` e percent-encoding são nomes literais: não achados, e nunca o ``.env``."""
    asm = _assembly()

    sem = await asm.service.create_agent(_config("s", doc_name, SEM))
    hier = await asm.service.create_agent(_config("h", doc_name, HIER))

    assert all(SEGREDO not in "".join(c) for c in vector_store.collections.values())
    assert all(SEGREDO not in n.content for n in asm.tree._nodes)
    assert not any(t for t in (hier.tools or []))
    assert sem.id == "s"


# ── subdiretório válido ──────────────────────────────────────────────


@pytest.mark.usefixtures("workspace")
async def test_doc_name_com_subdiretorio_valido_funciona_nos_dois_caminhos(
    vector_store: FakeVectorStore, offline_knowledge: OfflineKnowledge
):
    asm = _assembly()
    (Path("docs") / "manuais" / "hier.md").write_text(
        f"# Guia\n\nIntro.\n\n## Senha\n\n{GUIA} senha do subdiretório.\n", encoding="utf-8"
    )

    sem = await asm.service.create_agent(_config("sub-sem", "manuais/guia.md", SEM))
    hier = await asm.service.create_agent(_config("sub-hier", "manuais/hier.md", HIER))
    _attach(sem)
    brain = _attach(hier, "senha")

    assert sem.knowledge is not None
    assert vector_store.collections["rag_sub-sem"] == [p for p in DOCS["manuais/guia.md"].split("\n\n") if p.strip()]
    assert {n.doc_name for n in asm.tree._nodes} == {"manuais/hier.md"}
    entrypoint = _search_entrypoint(hier)
    assert GUIA in await entrypoint(query="senha")
    assert asm.logger.messages("warning", "error") == []
    assert brain.calls == []


@pytest.mark.usefixtures("workspace")
async def test_doc_name_apontando_para_diretorio_dentro_de_docs_nao_derruba_o_agente():
    """Review R1: diretório é recusado (o insert do agno o percorreria seguindo symlinks)."""
    asm = _assembly()

    hier = await asm.service.create_agent(_config("dir-hier", "manuais", HIER))
    sem = await asm.service.create_agent(_config("dir-sem", "manuais/", SEM))

    assert not (hier.tools or [])
    assert sem.knowledge is None
    rejected = [r for r in asm.logger.records if r.level == "error"]
    assert [r.message for r in rejected] == ["doc_name do RAG rejeitado; RAG do agente desativado"] * 2
    assert [r.context["agent_id"] for r in rejected] == ["dir-hier", "dir-sem"]
    assert all("diretório" in r.context["reason"] for r in rejected)


# ── mesmo arquivo em dois agentes ────────────────────────────────────


@pytest.mark.usefixtures("workspace")
async def test_mesmo_arquivo_em_dois_agentes_semanticos_usa_colecoes_separadas(
    vector_store: FakeVectorStore, offline_knowledge: OfflineKnowledge
):
    asm = _assembly()

    a = await asm.service.create_agent(_config("copia-a", "financeiro.md", SEM))
    b = await asm.service.create_agent(_config("copia-b", "financeiro.md", SEM))

    assert a.knowledge is not None and b.knowledge is not None
    assert a.knowledge.vector_db is not b.knowledge.vector_db
    assert set(vector_store.collections) == {"rag_copia-a", "rag_copia-b"}
    assert vector_store.collections["rag_copia-a"] == vector_store.collections["rag_copia-b"]


@pytest.mark.usefixtures("workspace")
async def test_mesmo_arquivo_em_dois_agentes_hierarquicos_compartilha_a_arvore_sequencial():
    """Comportamento registrado (roadmap, "Fica de fora"): árvore indexada por doc_name, indexada uma vez."""
    asm = _assembly()

    a = await asm.service.create_agent(_config("h-a", "hier-a.md", HIER))
    nodes_after_first = len(asm.tree._nodes)
    b = await asm.service.create_agent(_config("h-b", "hier-a.md", HIER))

    assert nodes_after_first > 0 and len(asm.tree._nodes) == nodes_after_first
    assert "Documento já indexado — skip" in asm.logger.messages("info")
    for agent in (a, b):
        entrypoint = _search_entrypoint(agent)
        assert HIER_A in await entrypoint(query="reembolso")


class _StartCountingLogger(RecordingLogger):
    """Sinaliza quando N indexações chegaram ao serviço (começaram ou esperam a em andamento).

    Sem depender de tempo: com o single-flight por ``doc_name`` (BUG-F1-07-QA-1), a 2ª
    chamada não começa, espera; o log de espera também conta.
    """

    _ARRIVALS = ("Iniciando indexação hierárquica", "Indexação do documento em andamento; aguardando")

    def __init__(self, expected_starts: int) -> None:
        super().__init__()
        self.all_started = threading.Event()
        self._expected = expected_starts

    def _log(self, level: str, message: str, kwargs: dict[str, Any]) -> None:
        super()._log(level, message, kwargs)
        if sum(self.messages("info", "debug").count(m) for m in self._ARRIVALS) >= self._expected:
            self.all_started.set()


@pytest.mark.usefixtures("workspace")
async def test_mesmo_arquivo_hierarquico_em_startup_concorrente_nao_duplica_a_arvore():
    """O startup cria os agentes com ``asyncio.gather``: dois agentes no mesmo arquivo não podem indexá-lo 2x.

    Determinístico: o embedding do 1º agente só prossegue quando a 2ª indexação também chegou
    ao serviço (começou ou está esperando a 1ª); timeout de 10 s só como rede de segurança.
    """
    solo = _assembly()
    await solo.service.create_agent(_config("so-um", "hier-a.md", HIER))
    expected = len(solo.tree._nodes)
    logger = _StartCountingLogger(expected_starts=2)
    embedders = KeywordEmbedderFactory(wait_for_loop=lambda: logger.all_started.wait(10.0))
    asm = _assembly(embedders=embedders, logger=logger)

    agents = await asyncio.gather(
        asm.service.create_agent(_config("h-a", "hier-a.md", HIER)),
        asm.service.create_agent(_config("h-b", "hier-a.md", HIER)),
    )

    assert logger.all_started.is_set(), "a 2ª indexação não chegou ao serviço: o teste não exercitou a corrida"
    assert len(asm.tree._nodes) == expected, "árvore duplicada: busca devolveria o mesmo trecho 2x"
    text = await _search_entrypoint(agents[0])(query="reembolso")
    assert text.count(HIER_A) == 1


# ── event loop livre durante a montagem e a busca ────────────────────


class LoopProbe:
    """Prova que o event loop andou: uma task no loop sinaliza uma ``threading.Event`` e a chamada
    síncrona sob teste (que roda numa thread, se estiver certo) espera o sinal. Se a chamada
    bloqueasse o loop, a task não rodaria e a espera estouraria o timeout (sem depender de tempo)."""

    def __init__(self, timeout: float = 10.0) -> None:
        self._timeout = timeout
        self._alive = threading.Event()
        self.results: list[bool] = []
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        self._task = asyncio.get_running_loop().create_task(self._beat())

    async def _beat(self) -> None:
        await asyncio.sleep(0.01)
        self._alive.set()

    def block_like_io(self) -> None:
        self.results.append(self._alive.wait(timeout=self._timeout))

    async def stop(self) -> None:
        if self._task:
            await self._task


async def test_controle_sonda_detecta_chamada_que_bloqueia_o_loop():
    """Controle negativo: bloquear o loop (chamar a espera na própria thread do loop) é detectado."""
    probe = LoopProbe(timeout=0.3)
    probe.start()

    probe.block_like_io()  # roda no loop: a task do sinal não consegue rodar
    await probe.stop()

    assert probe.results == [False]


@pytest.mark.usefixtures("workspace")
async def test_event_loop_nao_bloqueia_na_montagem_do_knowledge_semantico(vector_store: FakeVectorStore):
    probe = LoopProbe()
    vector_store.on_blocking_call = probe.block_like_io  # exists() e insert() do Knowledge
    asm = _assembly()
    probe.start()

    agent = await asm.service.create_agent(_config("lento", "financeiro.md", SEM))
    await probe.stop()

    assert agent.knowledge is not None
    assert probe.results and all(probe.results), "o event loop ficou parado durante a montagem do Knowledge"


@pytest.mark.usefixtures("workspace")
async def test_event_loop_nao_bloqueia_na_indexacao_nem_na_busca_hierarquica():
    probe = LoopProbe()
    asm = _assembly(embedders=KeywordEmbedderFactory(wait_for_loop=probe.block_like_io))
    probe.start()

    agent = await asm.service.create_agent(_config("lento-h", "hier-a.md", HIER))
    await probe.stop()
    indexing_checks = list(probe.results)
    probe.results.clear()
    probe._alive.clear()
    probe.start()
    entrypoint = _search_entrypoint(agent)
    text = await entrypoint(query="reembolso")
    await probe.stop()

    assert HIER_A in text
    assert len(indexing_checks) >= 3 and all(indexing_checks), "loop parado durante a indexação"
    assert probe.results == [True], "loop parado durante o embedding da query"


# ── resumo: modelo que falha vira fallback com log e sem stack ao cliente ──


class _ExplodingModel(FakeChatModel):
    def _next_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        raise RuntimeError("falha-interna-do-provedor token=sk-NAO-VAZAR")


class _ExplodingFactory(FakeModelFactory):
    def create_model(self, config: ModelConfig) -> FakeChatModel:
        self.created.append(factory_call(config))
        return _ExplodingModel(id=config.model_id, provider=config.provider)


@pytest.mark.usefixtures("workspace")
async def test_resumo_usa_o_modelo_na_indexacao_do_agente_montado():
    asm = _assembly(summary_models=FakeModelFactory(responses=["resumo-do-modelo"] * 50))

    await asm.service.create_agent(_config("h", "hier-a.md", HIER))

    internal = [n for n in asm.tree._nodes if n.children_ids]
    assert internal and all(n.summary == "resumo-do-modelo" for n in internal)
    assert asm.logger.messages("warning", "error") == []


@pytest.mark.usefixtures("workspace")
async def test_modelo_de_resumo_que_falha_cai_no_fallback_com_log_e_sem_stack_ao_cliente(
    client_for: Callable[[list[Agent]], TestClient],
):
    asm = _assembly(summary_models=_ExplodingFactory())
    agent = await asm.service.create_agent(_config("h", "hier-a.md", HIER))
    brain = _attach(agent, "reembolso")
    client = client_for([agent])

    response = client.post("/agents/h/runs", data={"message": "pergunta", "stream": "false"}, headers=RUN)

    assert response.status_code == 200, response.text
    for leak in ("Traceback", "RuntimeError", "sk-NAO-VAZAR", "falha-interna"):
        assert leak not in response.text
    assert HIER_A in _tool_results(brain)  # a busca segue funcionando com o resumo truncado
    internal = [n for n in asm.tree._nodes if n.children_ids]
    assert internal and all(n.summary == n.content[:200] for n in internal)
    warnings = [r for r in asm.logger.records if r.message == "Fallback de sumário: falha ao chamar o modelo"]
    assert len(warnings) == len(internal)
    assert {r.context["error_type"] for r in warnings} == {"RuntimeError"}
    assert asm.logger.messages("error") == []
