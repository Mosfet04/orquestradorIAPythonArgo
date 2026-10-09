"""QA F1-06: o guardrail de ``user_id`` e a ausência de ``user_id`` fixo, sem montar o AgentOS."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from agno.agent import Agent
from agno.exceptions import CheckTrigger, InputCheckError
from agno.run.agent import RunInput
from agno.team import Team

from src.application.services.agent_factory_service import (
    AgentFactoryService,
    UserIdRequiredGuardrail,
    requires_user_id,
)
from src.application.services.team_factory_service import TeamFactoryService
from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.team_config import TeamConfig
from tests.fakes import (
    FakeChatModel,
    FakeEmbedderFactory,
    FakeModelFactory,
    InMemoryToolRepository,
    RecordingLogger,
)

SRC = Path(__file__).resolve().parents[2] / "src"


class _NoTools:
    async def create_tools_from_configs(self, configs: list[object]) -> list[object]:
        return []


async def _agent(agent_id: str, *, memory: bool) -> Agent:
    service = AgentFactoryService(
        db_url="mongodb://mongo.invalid:27017",
        logger=RecordingLogger(),
        model_factory=FakeModelFactory(),
        embedder_factory=FakeEmbedderFactory(),
        tool_factory=_NoTools(),  # type: ignore[arg-type]
        tool_repository=InMemoryToolRepository([]),
    )
    return await service.create_agent(
        AgentConfig(
            id=agent_id, nome=agent_id, factory_ia_model="fake", model="m", descricao="d", prompt="p",
            user_memory_active=memory,
        )
    )


def _team(team_id: str, members: list[Agent], *, memory: bool) -> Team:
    service = TeamFactoryService(
        db_url="mongodb://mongo.invalid:27017", logger=RecordingLogger(), model_factory=FakeModelFactory()
    )
    return service.create_team(
        TeamConfig(
            id=team_id, nome=team_id, factory_ia_model="fake", model="m",
            member_ids=[m.id for m in members if m.id], mode="route", user_memory_active=memory,
        ),
        members,
    )


# Ausente, em branco, sem nenhum caractere visível (espaço, controle e formatação unicode, que
# ``str.strip`` não remove), o "default" reservado do fallback do agno e não-string (o AG-UI passa
# ``forwardedProps.user_id`` sem validar tipo). Nenhum caso pode levantar outra exceção: o agno
# engole exceção de pre-hook que não seja ``InputCheckError`` e o run seguiria.
BAD_USER_IDS: list[object] = [
    None, "", " ", "\t\n", "\u00a0", "\u3000",
    "\u200b", "\ufeff", "\u2060", "\x00", "\u200b \ufeff\t",
    "default",
    0, False, True, 123, 1.5, {}, [], ["ana"], {"id": "ana"}, b"ana",
]


@pytest.mark.parametrize("bad", BAD_USER_IDS, ids=repr)
async def test_guardrail_recusa_user_id_invalido_nos_dois_modos(bad: object) -> None:
    guardrail = UserIdRequiredGuardrail("ag-1")
    run_input = RunInput(input_content="oi")

    with pytest.raises(InputCheckError) as sync_error:
        guardrail.check(run_input, user_id=bad)  # type: ignore[arg-type]
    with pytest.raises(InputCheckError) as async_error:
        await guardrail.async_check(run_input, user_id=bad)  # type: ignore[arg-type]

    for caught in (sync_error, async_error):
        assert "ag-1" in str(caught.value) and "user_id" in str(caught.value)
        assert caught.value.check_trigger == CheckTrigger.VALIDATION_FAILED


# Sem normalização: " ana " é outro usuário que "ana"; "Default" não é o id reservado.
@pytest.mark.parametrize("good", ["ana", " ana ", "日本語", "x" * 10_000, "0", "Default", "default ", "\u200bana"])
async def test_guardrail_aceita_qualquer_user_id_nao_branco(good: str) -> None:
    guardrail = UserIdRequiredGuardrail("ag-1")
    guardrail.check(RunInput(input_content="oi"), user_id=good)
    await guardrail.async_check(RunInput(input_content="oi"), user_id=good)


async def test_agent_com_memoria_tem_um_guardrail_proprio_e_nenhum_user_id_fixo() -> None:
    first, second = await _agent("a1", memory=True), await _agent("a2", memory=True)

    assert first.user_id is None and second.user_id is None
    assert first.pre_hooks and second.pre_hooks
    (g1,), (g2,) = first.pre_hooks, second.pre_hooks
    assert isinstance(g1, UserIdRequiredGuardrail) and isinstance(g2, UserIdRequiredGuardrail)
    assert g1 is not g2 and (g1.entity_id, g2.entity_id) == ("a1", "a2")


async def test_agent_sem_memoria_nao_tem_pre_hook_nem_user_id() -> None:
    agent = await _agent("a1", memory=False)

    assert agent.user_id is None
    assert not agent.pre_hooks
    assert requires_user_id(agent) is False


@pytest.mark.parametrize(
    ("team_memory", "member_memory", "expected"),
    [(False, False, False), (True, False, True), (False, True, True), (True, True, True)],
)
async def test_team_exige_user_id_se_ele_ou_algum_membro_guarda_memoria(
    team_memory: bool, member_memory: bool, expected: bool
) -> None:
    quiet = await _agent("quieto", memory=False)
    member = await _agent("membro", memory=member_memory)

    team = _team("t", [quiet, member], memory=team_memory)

    assert team.user_id is None
    assert bool(team.pre_hooks) is expected
    assert requires_user_id(team) is team_memory


def test_requires_user_id_so_aceita_true_estrito() -> None:
    agent = Agent(model=FakeChatModel(), telemetry=False)
    assert requires_user_id(agent) is False
    agentic_only = Agent(model=FakeChatModel(), enable_agentic_memory=True, telemetry=False)
    assert requires_user_id(agentic_only) is True
    classic_only = Agent(model=FakeChatModel(), enable_user_memories=True, telemetry=False)
    assert requires_user_id(classic_only) is True


def test_nenhuma_chamada_em_src_passa_user_id_literal() -> None:
    """Regressão do B11: ``user_id="ava"`` (ou qualquer literal) em Agent/Team/qualquer chamada."""
    offenders: list[str] = []
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if isinstance(node, ast.Call):
                for keyword in node.keywords:
                    if keyword.arg == "user_id" and isinstance(keyword.value, ast.Constant):
                        offenders.append(f"{path.relative_to(SRC)}:{node.lineno}")
    assert offenders == []


@pytest.mark.parametrize("member_memory", [False, True])
@pytest.mark.parametrize("team_memory", [False, True])
async def test_guardrail_de_agent_e_team_sai_do_mesmo_predicado(team_memory: bool, member_memory: bool) -> None:
    """R4 (review): a decisão de pôr o guardrail usa ``requires_user_id`` nos dois factories."""
    member = await _agent("membro", memory=member_memory)
    team = _team("t", [member], memory=team_memory)

    assert bool(member.pre_hooks) is requires_user_id(member)
    assert bool(team.pre_hooks) is (requires_user_id(team) or requires_user_id(member))
