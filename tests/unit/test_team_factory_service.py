"""``TeamFactoryService``: o ``Team`` real do agno 2.5 montado a partir de ``TeamConfig``.

Sem ``patch`` de ``Team``/``MongoDb`` no módulo da fábrica: membros são ``Agent`` reais do agno, o
modelo vem do ``FakeModelFactory`` e o I/O do agno é cortado na borda pela fixture
``offline_knowledge`` (o ``MongoDb`` de sessões é o real, com cliente que só conecta no primeiro uso;
o F2-08 passa a injetá-lo).

Coberto em outro lugar (e por isso não repetido): kwargs completos de ``Team`` (``tests/golden``),
guardrail de ``user_id`` (``test_qa_f1_06_guardrail.py``) e o contrato do runtime
(``tests/contract/test_agent_runtime_contract.py``).
"""

from __future__ import annotations

import pytest
from agno.agent import Agent
from agno.db.mongo import MongoDb
from agno.team.mode import TeamMode

from src.domain.entities.team_config import TeamConfig
from src.infrastructure.runtime.agno.team_factory_service import TeamFactoryService
from tests.fakes import FakeChatModel, FakeModelFactory, RecordingLogger

pytestmark = pytest.mark.usefixtures("offline_knowledge")

DB_URL = "mongodb://mongo.test.invalid:27017"
DB_NAME = "testdb"


def _config(**overrides: object) -> TeamConfig:
    fields: dict[str, object] = {
        "id": "team-1",
        "nome": "Team Router",
        "factory_ia_model": "ollama",
        "model": "llama3.2:latest",
        "member_ids": ["agent-a", "agent-b"],
        "mode": "route",
    }
    fields.update(overrides)
    return TeamConfig(**fields)  # type: ignore[arg-type]


def _agent(agent_id: str) -> Agent:
    return Agent(id=agent_id, name=agent_id, model=FakeChatModel(id=f"m-{agent_id}"), telemetry=False)


def _service(logger: RecordingLogger, models: FakeModelFactory | None = None) -> TeamFactoryService:
    return TeamFactoryService(
        db_url=DB_URL, db_name=DB_NAME, logger=logger, model_factory=models or FakeModelFactory()
    )


def test_team_montado_carrega_identidade_modelo_membros_e_db_da_config() -> None:
    logger = RecordingLogger()
    models = FakeModelFactory()
    a, b, other = _agent("agent-a"), _agent("agent-b"), _agent("fora-do-team")
    config = _config(
        descricao="Roteia para o especialista",
        prompt="Escolha o membro certo.",
        model_params={"temperature": 0.1},
        base_url="https://gw.example.invalid/v1",
        api_key_ref="env:GW_API_KEY",
    )

    # a ordem dos membros é a de member_ids, não a da lista de agentes ativos
    team = _service(logger, models).create_team(config, [other, b, a])

    assert (team.id, team.name) == ("team-1", "Team Router")
    assert team.description == "Roteia para o especialista"
    assert team.instructions == "Escolha o membro certo."
    assert team.model is models.models[0]
    assert models.configs == [config.model_config]  # campos novos chegam à fábrica (F2-02)
    members = team.members
    assert isinstance(members, list)
    assert len(members) == 2 and members[0] is a and members[1] is b
    assert isinstance(team.db, MongoDb)
    assert (team.db.db_url, team.db.db_name) == (DB_URL, DB_NAME)
    assert logger.messages("warning", "error") == []
    assert [(r.message, r.context) for r in logger.records if r.level == "info"] == [
        ("Team criado", {"team_id": "team-1", "mode": "route", "member_count": 2})
    ]


def test_team_sem_descricao_nem_prompt_fica_com_descricao_vazia_e_sem_instrucoes() -> None:
    team = _service(RecordingLogger()).create_team(_config(), [_agent("agent-a"), _agent("agent-b")])

    assert team.description == ""
    assert team.instructions is None


@pytest.mark.parametrize(
    ("mode", "expected", "respond_directly"),
    [
        ("route", TeamMode.route, True),
        ("coordinate", TeamMode.coordinate, False),
        ("broadcast", TeamMode.broadcast, False),
        ("tasks", TeamMode.tasks, False),
    ],
)
def test_modo_do_team_segue_a_config_e_so_o_route_responde_direto(
    mode: str, expected: TeamMode, respond_directly: bool
) -> None:
    team = _service(RecordingLogger()).create_team(_config(mode=mode), [_agent("agent-a"), _agent("agent-b")])

    assert team.mode is expected
    assert team.respond_directly is respond_directly


@pytest.mark.parametrize(
    ("user_memory", "summary"), [(False, False), (True, False), (False, True), (True, True)]
)
def test_memoria_e_sumario_do_team_seguem_a_config(user_memory: bool, summary: bool) -> None:
    team = _service(RecordingLogger()).create_team(
        _config(member_ids=["agent-a"], user_memory_active=user_memory, summary_active=summary),
        [_agent("agent-a")],
    )

    assert (team.enable_user_memories, team.enable_agentic_memory) == (user_memory, user_memory)
    assert team.enable_session_summaries is summary


def test_membro_que_nao_esta_entre_os_agentes_ativos_fica_de_fora_com_aviso() -> None:
    logger = RecordingLogger()
    a = _agent("agent-a")

    team = _service(logger).create_team(_config(member_ids=["agent-a", "agent-missing"]), [a])

    assert isinstance(team.members, list) and len(team.members) == 1 and team.members[0] is a
    assert [(r.message, r.context) for r in logger.records if r.level == "warning"] == [
        ("Membro não encontrado entre agentes ativos", {"team_id": "team-1", "member_id": "agent-missing"})
    ]


def test_team_sem_nenhum_membro_ativo_falha_antes_de_criar_o_modelo() -> None:
    logger = RecordingLogger()
    models = FakeModelFactory()

    with pytest.raises(ValueError, match="'team-1': nenhum membro válido"):
        _service(logger, models).create_team(_config(), [_agent("agent-x")])

    assert models.created == []
    assert [r.context["member_id"] for r in logger.records if r.level == "warning"] == ["agent-a", "agent-b"]
