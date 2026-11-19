"""F3-10: raciocínio fora do AG-UI por agente (``expose_reasoning=false``).

As tools de raciocínio se reconhecem pela origem (a ``PrivateReasoningTools`` que a fábrica põe no
agente com o campo desligado), não pelo nome: tool do usuário chamada ``think`` segue visível. O E2E
pelo AG-UI, com modelo scriptado, está em ``tests/security/test_agui_reasoning_visibility.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

from ag_ui.core import (
    BaseEvent,
    EventType,
    RunFinishedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from agno.agent import Agent
from agno.models.response import ToolExecution
from agno.run.agent import RunContentEvent, ToolCallCompletedEvent, ToolCallStartedEvent
from agno.run.team import ToolCallStartedEvent as TeamToolCallStartedEvent
from agno.team import Team
from agno.tools import Toolkit
from agno.tools.function import Function
from agno.tools.reasoning import ReasoningTools

from src.infrastructure.runtime.agno.agui_router import AgnoEvent, filter_agui_events
from src.infrastructure.runtime.agno.reasoning_visibility import (
    PrivateReasoningTools,
    private_reasoning_tools,
    track_private_reasoning_calls,
)
from tests.fakes import FakeChatModel, RecordingLogger

THOUGHT = "PENSAMENTO-PRIVADO-5512"


def _agent(agent_id: str, tools: list[object]) -> Agent:
    return Agent(id=agent_id, name=agent_id, model=FakeChatModel(), tools=tools, telemetry=False)


def _user_think(text: str) -> str:
    """Tool do usuário com o mesmo nome da de raciocínio."""
    return text


def _function(name: str) -> Function:
    return Function(name=name, entrypoint=_user_think)


# ── origem das tools de raciocínio ──────────────────────────────────


def test_agente_com_o_raciocinio_privado_esconde_think_e_analyze():
    agent = _agent("pensa", [PrivateReasoningTools(add_instructions=True)])

    assert private_reasoning_tools(agent) == {"pensa": frozenset({"think", "analyze"})}


def test_raciocinio_exposto_ou_ausente_nao_esconde_nada():
    exposed = _agent("exposto", [ReasoningTools(add_instructions=True)])
    plain = _agent("sem", [])
    no_tools = Agent(id="nada", name="nada", model=FakeChatModel(), telemetry=False)

    assert [private_reasoning_tools(a) for a in (exposed, plain, no_tools)] == [{}, {}, {}]


def test_tool_do_usuario_chamada_think_nao_e_confundida_com_a_de_raciocinio():
    """O agno registra o primeiro nome e pula o repetido (``agent/_tools.py``, ``parse_tools``): a ``think``
    do usuário, antes da toolkit, é a que roda; só a ``analyze`` é do raciocínio."""
    as_function = _agent("funcao", [_function("think"), PrivateReasoningTools()])
    as_callable = _agent("callable", [_named("analyze"), PrivateReasoningTools()])
    as_toolkit = _agent("toolkit", [Toolkit(name="meu", tools=[_named("think")]), PrivateReasoningTools()])
    without_reasoning = _agent("sem", [_function("think"), _function("analyze")])

    assert private_reasoning_tools(as_function) == {"funcao": frozenset({"analyze"})}
    assert private_reasoning_tools(as_callable) == {"callable": frozenset({"think"})}
    assert private_reasoning_tools(as_toolkit) == {"toolkit": frozenset({"analyze"})}
    assert private_reasoning_tools(without_reasoning) == {}


def test_toolkit_de_raciocinio_antes_da_tool_do_usuario_vence_o_nome():
    agent = _agent("antes", [PrivateReasoningTools(), _function("think")])

    assert private_reasoning_tools(agent) == {"antes": frozenset({"think", "analyze"})}


def test_em_team_vale_a_configuracao_de_cada_membro():
    private = _agent("privado", [PrivateReasoningTools()])
    public = _agent("publico", [ReasoningTools()])
    inner = Team(id="interno", name="interno", members=[_agent("fundo", [PrivateReasoningTools()])],
                 model=FakeChatModel(), telemetry=False)
    team = Team(id="time", name="time", members=[private, public, inner], model=FakeChatModel(), telemetry=False)

    assert private_reasoning_tools(team) == {
        "privado": frozenset({"think", "analyze"}),
        "fundo": frozenset({"think", "analyze"}),
    }


def _named(name: str):
    def tool(text: str) -> str:
        """Tool do usuário."""
        return text

    tool.__name__ = name
    return tool


# ── marcação das chamadas pelo stream do agno ───────────────────────


def _started(agent_id: str, call_id: str, name: str) -> ToolCallStartedEvent:
    tool = ToolExecution(tool_call_id=call_id, tool_name=name, tool_args={"thought": THOUGHT})
    return ToolCallStartedEvent(agent_id=agent_id, run_id="r1", tool=tool)


async def _track(chunks: Sequence[AgnoEvent], private: dict[str, frozenset[str]]) -> tuple[list[AgnoEvent], set[str]]:
    async def source() -> AsyncIterator[AgnoEvent]:
        for chunk in chunks:
            yield chunk

    hidden: set[str] = set()
    passed = [chunk async for chunk in track_private_reasoning_calls(source(), private, hidden)]
    return passed, hidden


async def test_marca_so_as_chamadas_de_raciocinio_do_agente_que_esconde():
    chunks: list[AgnoEvent] = [
        _started("privado", "c-think", "think"),
        _started("privado", "c-busca", "busca"),
        _started("publico", "c-outro-think", "think"),
        TeamToolCallStartedEvent(
            team_id="time", run_id="r1", tool=ToolExecution(tool_call_id="c-t", tool_name="think")
        ),
        ToolCallCompletedEvent(
            agent_id="privado", run_id="r1", tool=ToolExecution(tool_call_id="c-analyze", tool_name="analyze")
        ),
        RunContentEvent(agent_id="privado", run_id="r1", content="resposta"),
    ]

    passed, hidden = await _track(chunks, {"privado": frozenset({"think", "analyze"})})

    assert passed == chunks  # nada some do stream do agno: o raciocínio segue no run e na sessão
    assert hidden == {"c-think", "c-analyze"}


async def test_chamada_sem_id_nao_e_marcada():
    chunk = ToolCallStartedEvent(agent_id="privado", run_id="r1", tool=ToolExecution(tool_name="think"))

    _, hidden = await _track([chunk], {"privado": frozenset({"think"})})

    assert hidden == set()


# ── filtro de saída ─────────────────────────────────────────────────


def _tool_call(call_id: str, name: str, args: str, result: str) -> list[BaseEvent]:
    return [
        ToolCallStartEvent(type=EventType.TOOL_CALL_START, tool_call_id=call_id, tool_call_name=name),
        ToolCallArgsEvent(type=EventType.TOOL_CALL_ARGS, tool_call_id=call_id, delta=args),
        ToolCallEndEvent(type=EventType.TOOL_CALL_END, tool_call_id=call_id),
        ToolCallResultEvent(
            type=EventType.TOOL_CALL_RESULT, tool_call_id=call_id, message_id=call_id, content=result, role="tool"
        ),
    ]


async def test_filtro_descarta_os_tool_call_marcados_e_mantem_o_resto_na_ordem():
    logger = RecordingLogger()
    events: list[BaseEvent] = [
        *_tool_call("c-think", "think", f'{{"thought": "{THOUGHT}"}}', THOUGHT),
        *_tool_call("c-busca", "busca", '{"q": "x"}', "achado"),
        TextMessageStartEvent(type=EventType.TEXT_MESSAGE_START, message_id="m", role="assistant"),
        TextMessageContentEvent(type=EventType.TEXT_MESSAGE_CONTENT, message_id="m", delta="resposta final"),
        TextMessageEndEvent(type=EventType.TEXT_MESSAGE_END, message_id="m"),
        RunFinishedEvent(type=EventType.RUN_FINISHED, thread_id="t1", run_id="r1"),
    ]

    async def source() -> AsyncIterator[BaseEvent]:
        for event in events:
            yield event

    out = [
        e
        async for e in filter_agui_events(
            source(), [], entity_id="a1", run_id="r1", logger=logger, hidden_tool_call_ids={"c-think"}
        )
    ]

    assert out == events[4:]
    assert THOUGHT not in "".join(e.model_dump_json() for e in out)
    hidden_logs = [r.context for r in logger.records if r.level == "debug"]
    assert [c["event_type"] for c in hidden_logs] == ["TOOL_CALL_START", "TOOL_CALL_ARGS", "TOOL_CALL_END",
                                                      "TOOL_CALL_RESULT"]
    assert all(THOUGHT not in str(r.context) and THOUGHT not in r.message for r in logger.records)


async def test_filtro_sem_marcados_e_o_do_f3_05():
    events: list[BaseEvent] = [
        *_tool_call("c-think", "think", f'{{"thought": "{THOUGHT}"}}', THOUGHT),
        RunFinishedEvent(type=EventType.RUN_FINISHED, thread_id="t1", run_id="r1"),
    ]

    async def source() -> AsyncIterator[BaseEvent]:
        for event in events:
            yield event

    out = [e async for e in filter_agui_events(source(), [], entity_id="a1", run_id="r1", logger=RecordingLogger())]

    assert out == events
