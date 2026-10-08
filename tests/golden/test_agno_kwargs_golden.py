"""Golden: kwargs que a montagem de agentes/teams passa a ``agno.Agent``/``agno.Team``.

Rede de segurança da F2 (montagem migra para o adapter ``AgnoRuntime``). Se um kwarg
mudar, o teste falha com o diff; atualização só com ``--update-golden``.

Na F2, só ``_Assembly`` muda (de onde vêm as fábricas); cenários e snapshots ficam.
"""

from __future__ import annotations

import importlib
from dataclasses import dataclass
from pathlib import Path

import pytest

from src.application.services.agent_factory_service import AgentFactoryService
from src.application.services.document_indexing_service import DocumentIndexingService
from src.application.services.knowledge_search_factory import KnowledgeSearchFactory
from src.application.services.team_factory_service import TeamFactoryService
from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.rag_config import RagConfig, SearchStrategy
from src.domain.entities.team_config import TeamConfig
from src.domain.entities.tool import HttpMethod, ParameterType, Tool, ToolParameter
from src.domain.ports.summary_generator_port import ISummaryGenerator
from src.infrastructure.http.http_tool_factory import HttpToolFactory
from src.infrastructure.parsers.text_document_parser import TextDocumentParser
from tests.fakes import (
    FakeChatModel,
    FakeEmbedderFactory,
    FakeModelFactory,
    InMemoryDocumentTreeRepository,
    InMemoryToolRepository,
    RecordingLogger,
)
from tests.fakes.models import FactoryCall
from tests.golden.conftest import AgnoCalls, GoldenSnapshot
from tests.golden.normalize import JsonValue, mask_url_userinfo, normalize

DB_URL = "mongodb://mongo.golden.invalid:27017"
DB_NAME = "golden_db"

MANUAL_MD = """# Manual do Produto

Visão geral do produto.

## Instalação

Passo a passo de instalação.

## Uso

### Login

Como entrar no sistema.

### Relatórios

Como gerar relatórios.
"""

TOOLS = [
    Tool(
        id="consulta-cep",
        name="Consulta CEP",
        description="Busca endereço pelo CEP",
        route="https://api.example.invalid/cep/{cep}",
        http_method=HttpMethod.GET,
        parameters=[ToolParameter(name="cep", type=ParameterType.STRING, description="CEP", required=True)],
        instructions="Use quando o usuário informar um CEP.",
    ),
    Tool(
        id="cria-pedido",
        name="Cria pedido",
        description="Cria um pedido",
        route="https://api.example.invalid/pedidos",
        http_method=HttpMethod.POST,
        parameters=[
            ToolParameter(name="produto", type=ParameterType.STRING, description="SKU", required=True),
            ToolParameter(name="quantidade", type=ParameterType.INTEGER, description="Quantidade"),
        ],
        headers={"X-Origem": "orquestrador"},
    ),
    Tool(
        id="tool-inativa",
        name="Inativa",
        description="Não deve ser montada",
        route="https://api.example.invalid/x",
        http_method=HttpMethod.GET,
        parameters=[],
        active=False,
    ),
]


def _agent(agent_id: str, **overrides: object) -> AgentConfig:
    fields: dict[str, object] = {
        "id": agent_id,
        "nome": f"Agente {agent_id}",
        "factory_ia_model": "openai",
        "model": "gpt-4o-mini",
        "descricao": f"Descrição de {agent_id}",
        "prompt": f"Você é o {agent_id}.",
    }
    fields.update(overrides)
    return AgentConfig(**fields)  # type: ignore[arg-type]


AGENT_SCENARIOS = {
    "agent_minimo": _agent("minimo"),
    "agent_com_tools": _agent(
        "com-tools",
        factory_ia_model="anthropic",
        model="claude-x",
        tools_ids=["consulta-cep", "cria-pedido", "tool-inativa", "inexistente"],
        user_memory_active=True,
        summary_active=True,
    ),
    "agent_rag_semantic": _agent(
        "rag-semantic",
        factory_ia_model="ollama",
        model="llama3.2:latest",
        rag_config=RagConfig(
            active=True,
            doc_name="manual.md",
            model="nomic-embed-text:latest",
            factory_ia_model="ollama",
            search_strategy=SearchStrategy.SEMANTIC,
        ),
    ),
    "agent_rag_hierarchical": _agent(
        "rag-hierarchical",
        rag_config=RagConfig(
            active=True,
            doc_name="manual.md",
            model="text-embedding-3-small",
            factory_ia_model="openai",
            search_strategy=SearchStrategy.HIERARCHICAL,
        ),
    ),
}


class _FixedSummary(ISummaryGenerator):
    async def generate_summary(self, content: str) -> str:
        return f"resumo: {content[:30]}"


@dataclass
class _Assembly:
    """Monta as fábricas reais com fakes nas portas. Único ponto a ajustar na F2."""

    logger: RecordingLogger
    model_factory: FakeModelFactory
    embedder_factory: FakeEmbedderFactory
    agents: AgentFactoryService
    teams: TeamFactoryService

    @classmethod
    def build(cls) -> _Assembly:
        logger = RecordingLogger()
        model_factory = FakeModelFactory()
        embedder_factory = FakeEmbedderFactory(dimensions=8)
        tree_repo = InMemoryDocumentTreeRepository()
        agents = AgentFactoryService(
            db_url=DB_URL,
            db_name=DB_NAME,
            logger=logger,
            model_factory=model_factory,
            embedder_factory=embedder_factory,
            tool_factory=HttpToolFactory(logger=logger),
            tool_repository=InMemoryToolRepository(TOOLS),
            indexing_service=DocumentIndexingService(
                parser=TextDocumentParser(),
                tree_repository=tree_repo,
                summary_generator=_FixedSummary(),
                embedder_factory=embedder_factory,
                logger=logger,
            ),
            search_factory=KnowledgeSearchFactory(tree_repository=tree_repo, logger=logger),
        )
        teams = TeamFactoryService(db_url=DB_URL, db_name=DB_NAME, logger=logger, model_factory=model_factory)
        return cls(
            logger=logger,
            model_factory=model_factory,
            embedder_factory=embedder_factory,
            agents=agents,
            teams=teams,
        )

    def problems(self) -> list[str]:
        return [f"{r.level}: {r.message}" for r in self.logger.records if r.level in ("warning", "error")]

    @staticmethod
    def _factory_calls(created: list[FactoryCall]) -> list[JsonValue]:
        return [{"provider": p, "model_id": m, "kwargs": normalize(kw)} for p, m, kw in created]

    def factory_calls(self) -> dict[str, JsonValue]:
        """O que a montagem pediu às fábricas (provider, id, kwargs): o modelo real sai daí."""
        return {
            "model": self._factory_calls(self.model_factory.created),
            "embedder": self._factory_calls(self.embedder_factory.created),
        }


@pytest.fixture
def workdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A montagem lê ``docs/<doc_name>`` relativo ao cwd."""
    (tmp_path / "docs").mkdir()
    (tmp_path / "docs" / "manual.md").write_text(MANUAL_MD, encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


@pytest.mark.usefixtures("workdir")
@pytest.mark.parametrize("scenario", sorted(AGENT_SCENARIOS))
async def test_kwargs_do_agent_batem_com_o_golden(scenario: str, agno_calls: AgnoCalls, golden: GoldenSnapshot):
    assembly = _Assembly.build()

    agent = await assembly.agents.create_agent(AGENT_SCENARIOS[scenario])

    assert agent.id == AGENT_SCENARIOS[scenario].id
    golden.check(
        scenario,
        {
            "Agent": agno_calls.of("Agent"),
            "Team": agno_calls.of("Team"),
            "knowledge_inserts": agno_calls.knowledge_inserts,
            "factory_calls": assembly.factory_calls(),
            "log_warnings_errors": assembly.problems(),  # type: ignore[dict-item]
        },
    )


TEAM_CONFIGS = [
    TeamConfig(
        id="time-roteador",
        nome="Time Roteador",
        factory_ia_model="openai",
        model="gpt-4o",
        member_ids=["minimo", "com-tools", "fantasma"],
        mode="route",
        descricao="Roteia para o especialista",
        prompt="Escolha o membro certo.",
    ),
    TeamConfig(
        id="time-coordenador",
        nome="Time Coordenador",
        factory_ia_model="anthropic",
        model="claude-x",
        member_ids=["com-tools"],
        mode="coordinate",
        user_memory_active=False,
        summary_active=True,
    ),
]


@pytest.mark.usefixtures("workdir")
async def test_kwargs_do_team_batem_com_o_golden(agno_calls: AgnoCalls, golden: GoldenSnapshot):
    assembly = _Assembly.build()
    members = [
        await assembly.agents.create_agent(AGENT_SCENARIOS["agent_minimo"]),
        await assembly.agents.create_agent(AGENT_SCENARIOS["agent_com_tools"]),
    ]

    teams = [assembly.teams.create_team(cfg, members) for cfg in TEAM_CONFIGS]

    assert [t.id for t in teams] == ["time-roteador", "time-coordenador"]
    golden.check(
        "team",
        {
            "Team": agno_calls.of("Team"),
            "factory_calls": assembly.factory_calls(),
            "log_warnings_errors": assembly.problems(),  # type: ignore[dict-item]
        },
    )


@pytest.mark.usefixtures("workdir")
async def test_agent_e_team_nunca_sao_criados_com_telemetria_ligada(agno_calls: AgnoCalls):
    """F1-03: telemetria da Agno explícita e desligada em toda montagem (não só pelo env)."""
    assembly = _Assembly.build()
    agents = [await assembly.agents.create_agent(cfg) for cfg in AGENT_SCENARIOS.values()]
    for cfg in TEAM_CONFIGS:
        assembly.teams.create_team(cfg, agents)

    calls = agno_calls.of("Agent") + agno_calls.of("Team")
    assert len(calls) == len(AGENT_SCENARIOS) + len(TEAM_CONFIGS)
    assert [call.get("telemetry") for call in calls] == [False] * len(calls)


# ── o próprio mecanismo de golden ───────────────────────────────────


def test_espiao_pega_agent_construido_por_qualquer_caminho_de_import(agno_calls: AgnoCalls):
    """O espião está na classe: vale para o adapter da F2, que importará o agno de outro módulo."""
    agent_cls = importlib.import_module("agno.agent.agent").Agent

    agent_cls(id="qualquer", model=FakeChatModel(id="m"), telemetry=False)

    assert agno_calls.of("Agent") == [
        {
            "id": "qualquer",
            "model": {"__type__": "fake", "id": "m", "provider": "fake"},
            "telemetry": False,
        }
    ]


def test_normalize_identifica_fakes_por_tipo_estavel_e_nao_pelo_caminho_do_modulo():
    """Mover os fakes de módulo (F2) não pode exigir ``--update-golden``."""
    embedder = FakeEmbedderFactory(dimensions=4).create_model("ollama", "nomic")

    assert normalize(FakeChatModel(id="m", provider="openai")) == {"__type__": "fake", "id": "m", "provider": "openai"}
    assert normalize(embedder) == {"__type__": "fake", "id": "nomic", "provider": "ollama", "dimensions": 4}


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("mongodb://user:s3nh4@db.invalid:27017/app", "mongodb://***@db.invalid:27017/app"),
        ("mongodb+srv://user@cluster.invalid/?retryWrites=true", "mongodb+srv://***@cluster.invalid/?retryWrites=true"),
        ("mongodb://u:p%40ss@h1.invalid:1,h2.invalid:2/db", "mongodb://***@h1.invalid:1,h2.invalid:2/db"),
        ("mongodb://user:pa#ss@db.invalid:27017/app", "mongodb://***@db.invalid:27017/app"),
        ("mongodb://db.invalid:27017/db?authSource=a@b", "mongodb://db.invalid:27017/db?authSource=a@b"),
        ("mongodb://db.invalid:27017", "mongodb://db.invalid:27017"),
        (None, None),
    ],
)
def test_mask_url_userinfo_esconde_so_as_credenciais(url: str | None, expected: str | None):
    assert mask_url_userinfo(url) == expected


# agno_calls: MongoClient com connect=False (MongoDb do agno criaria cliente real ao construir)
@pytest.mark.usefixtures("agno_calls")
def test_normalize_nao_grava_credenciais_das_urls_de_conexao():
    from agno.db.mongo import MongoDb as MongoAgentDb
    from agno.vectordb.mongodb import MongoDb as MongoVectorDb

    url = "mongodb://user:s3nh4@db.invalid:27017/app"
    embedder = FakeEmbedderFactory().create_model("x", "y")

    db = normalize(MongoAgentDb(db_url=url, db_name=DB_NAME))
    vector = normalize(MongoVectorDb(collection_name="rag", db_url=url, database=DB_NAME, embedder=embedder))

    assert isinstance(db, dict) and isinstance(vector, dict)
    assert db["db_url"] == vector["connection_string"] == "mongodb://***@db.invalid:27017/app"


def test_golden_falha_quando_um_kwarg_muda_sem_update(tmp_path: Path):
    saved = {"Agent": [{"num_history_runs": 5, "markdown": True}]}
    GoldenSnapshot(tmp_path, update=True).check("cenario", saved)

    GoldenSnapshot(tmp_path, update=False).check("cenario", saved)  # igual: passa
    changed = {"Agent": [{"num_history_runs": 6, "markdown": True}]}
    with pytest.raises(pytest.fail.Exception, match=r'-      "num_history_runs": 5'):
        GoldenSnapshot(tmp_path, update=False).check("cenario", changed)


def test_golden_sem_snapshot_falha_e_nao_cria_arquivo(tmp_path: Path):
    with pytest.raises(pytest.fail.Exception, match="--update-golden"):
        GoldenSnapshot(tmp_path, update=False).check("novo", {"a": 1})
    assert not (tmp_path / "novo.json").exists()
