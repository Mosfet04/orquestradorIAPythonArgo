"""QA F1-06: o guardrail de ``user_id`` e a ausência de ``user_id`` fixo, sem montar o AgentOS."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import TypeVar

import pytest
from agno.agent import Agent
from agno.exceptions import CheckTrigger, InputCheckError
from agno.run.agent import RunInput
from agno.team import Team

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.team_config import TeamConfig
from src.infrastructure.runtime.agno.agent_factory_service import AgentFactoryService
from src.infrastructure.runtime.agno.team_factory_service import TeamFactoryService
from src.infrastructure.runtime.agno.user_id_guardrail import (
    UserIdRequiredGuardrail,
    requires_user_id,
)
from tests.fakes import (
    FakeChatModel,
    FakeEmbedderFactory,
    FakeModelFactory,
    InMemoryToolRepository,
    RecordingLogger,
)

SRC = Path(__file__).resolve().parents[2] / "src"
EntityT = TypeVar("EntityT", Agent, Team)


class _NoTools:
    async def create_tools_from_configs(self, configs: list[object]) -> list[object]:
        return []


async def _agent(agent_id: str, *, memory: bool) -> Agent:
    service = AgentFactoryService(
        db_url="mongodb://mongo.invalid:27017",
        session_db=None,
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
        session_db=None, logger=RecordingLogger(), model_factory=FakeModelFactory()
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
    classic_only = Agent(model=FakeChatModel(), update_memory_on_run=True, telemetry=False)
    assert requires_user_id(classic_only) is True


def _without_legacy_memory_flag(entity: EntityT) -> EntityT:
    """Tira o espelho legado ``enable_user_memories`` da instância, como no agno 3.x.

    No 2.5.8 o ``__init__`` copia ``update_memory_on_run`` para ``enable_user_memories``
    (``agno/agent/agent.py:504-508``, ``agno/team/_init.py:313-317``); no 3.1.12 o campo
    não existe. Sem a cópia, o atributo cai no default da classe (``None``) aqui e some lá.
    """
    vars(entity).pop("enable_user_memories", None)
    return entity


def test_agent_com_memoria_exige_user_id_sem_depender_de_enable_user_memories() -> None:
    """F3-02: o guardrail lê ``update_memory_on_run``; o renome do 3.x não o desliga em silêncio."""
    classic = _without_legacy_memory_flag(
        Agent(model=FakeChatModel(), update_memory_on_run=True, telemetry=False)
    )
    agentic = _without_legacy_memory_flag(
        Agent(model=FakeChatModel(), enable_agentic_memory=True, telemetry=False)
    )
    quiet = _without_legacy_memory_flag(Agent(model=FakeChatModel(), telemetry=False))

    assert (requires_user_id(classic), requires_user_id(agentic), requires_user_id(quiet)) == (
        True,
        True,
        False,
    )


def test_team_com_memoria_exige_user_id_sem_depender_de_enable_user_memories() -> None:
    """F3-02: Team com memória própria ou de membro recebe o guardrail sem o kwarg legado."""
    quiet = _without_legacy_memory_flag(Agent(id="quieto", model=FakeChatModel(), telemetry=False))
    member = _without_legacy_memory_flag(
        Agent(id="membro", model=FakeChatModel(), update_memory_on_run=True, telemetry=False)
    )
    own_memory = _without_legacy_memory_flag(
        Team(members=[quiet], model=FakeChatModel(), update_memory_on_run=True, telemetry=False)
    )

    by_member = _team("t", [quiet, member], memory=False)

    assert requires_user_id(own_memory) is True
    assert requires_user_id(by_member) is False
    assert by_member.pre_hooks
    (guardrail,) = by_member.pre_hooks
    assert isinstance(guardrail, UserIdRequiredGuardrail) and guardrail.entity_id == "t"


def _only_canonical_memory_flag(entity: EntityT) -> EntityT:
    """Deixa só ``update_memory_on_run``: tira também ``enable_agentic_memory`` (a factory liga os dois juntos).

    Sem isto o guardrail continuaria verdadeiro pelo flag agêntico e o teste não distinguiria o nome
    canônico do legado.
    """
    vars(entity).pop("enable_agentic_memory", None)
    return _without_legacy_memory_flag(entity)


@pytest.mark.parametrize(
    ("team_memory", "member_memory"),
    [(False, True), (True, False), (True, True)],
    ids=["so-membro", "so-team", "ambos"],
)
async def test_factories_ligam_memoria_pelo_nome_canonico(team_memory: bool, member_memory: bool) -> None:
    """F3-02: a factory grava ``update_memory_on_run``; sem o espelho legado o team ainda é guardado.

    Com ``team_memory=False`` e ``member_memory=True`` o guardrail do Team vem só de
    ``requires_user_id(membro)`` sobre o membro montado pela factory (não do flag do próprio Team).
    A factory liga ``enable_agentic_memory`` junto; os helpers o tiram para que só o nome canônico conte.
    """
    member = _only_canonical_memory_flag(await _agent("membro", memory=member_memory))
    quiet = _only_canonical_memory_flag(await _agent("quieto", memory=False))
    team = _only_canonical_memory_flag(_team("t", [quiet, member], memory=team_memory))

    assert (member.update_memory_on_run, quiet.update_memory_on_run) == (member_memory, False)
    assert team.update_memory_on_run is team_memory
    assert requires_user_id(member) is member_memory
    assert requires_user_id(team) is team_memory
    assert bool(member.pre_hooks) is member_memory
    assert not quiet.pre_hooks
    assert team.pre_hooks, "team com memória própria ou de membro precisa do guardrail"
    (guardrail,) = team.pre_hooks
    assert isinstance(guardrail, UserIdRequiredGuardrail) and guardrail.entity_id == "t"


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
