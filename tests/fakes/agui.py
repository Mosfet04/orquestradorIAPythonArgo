"""Leitor e validador de streams AG-UI (SSE) para os testes.

Parseia o corpo SSE com os modelos de ``ag_ui.core`` (o ``Event`` é uma união
discriminada por ``type``: evento malformado ou de tipo desconhecido levanta
``ValidationError``) e confere as regras de sequência do protocolo que o
``ag-ui-protocol`` 0.1.22 não verifica sozinho.
"""

from __future__ import annotations

from ag_ui.core import (
    BaseEvent,
    Event,
    EventType,
    RunErrorEvent,
    RunFinishedEvent,
    RunStartedEvent,
    TextMessageContentEvent,
    TextMessageEndEvent,
    TextMessageStartEvent,
    ToolCallArgsEvent,
    ToolCallEndEvent,
    ToolCallResultEvent,
    ToolCallStartEvent,
)
from pydantic import TypeAdapter

_EVENT = TypeAdapter(Event)
TERMINALS = (EventType.RUN_FINISHED, EventType.RUN_ERROR)


def parse_agui_sse(text: str) -> list[BaseEvent]:
    """Eventos do corpo SSE; exige o enquadramento ``data: <json>\\n\\n`` em todo bloco."""
    assert text == "" or text.endswith("\n\n"), "o stream deve terminar num bloco SSE completo"
    events: list[BaseEvent] = []
    for block in text.split("\n\n")[:-1] if text else []:
        lines = block.split("\n")
        assert len(lines) == 1 and lines[0].startswith("data: "), f"bloco SSE inesperado: {block[:80]!r}"
        events.append(_EVENT.validate_json(lines[0].removeprefix("data: ")))
    return events


def assert_valid_run(events: list[BaseEvent]) -> None:
    """Regras de ciclo de vida do AG-UI: RUN_STARTED primeiro, um só terminal, no fim;
    mensagens de texto e tool calls abertas e fechadas, na ordem."""
    assert events, "stream vazio"
    assert isinstance(events[0], RunStartedEvent), f"primeiro evento: {events[0].type}"
    terminals = [e for e in events if e.type in TERMINALS]
    assert len(terminals) == 1, f"terminais: {[e.type for e in terminals]}"
    assert events[-1] is terminals[0], "nada pode vir depois do evento terminal"
    assert sum(isinstance(e, RunStartedEvent) for e in events) == 1

    started = events[0]
    assert isinstance(started, RunStartedEvent)
    last = events[-1]
    if isinstance(last, RunFinishedEvent):
        assert (last.thread_id, last.run_id) == (started.thread_id, started.run_id)
    else:
        assert isinstance(last, RunErrorEvent) and last.message

    open_messages: set[str] = set()
    closed_messages: set[str] = set()
    tool_state: dict[str, str] = {}  # tool_call_id -> started | args | ended | result
    for event in events[1:-1]:
        if isinstance(event, TextMessageStartEvent):
            assert event.message_id not in open_messages | closed_messages, "message_id reaproveitado"
            open_messages.add(event.message_id)
        elif isinstance(event, TextMessageContentEvent):
            assert event.message_id in open_messages, "TEXT_MESSAGE_CONTENT fora de uma mensagem aberta"
            assert event.delta, "delta vazio é inválido no protocolo"
        elif isinstance(event, TextMessageEndEvent):
            assert event.message_id in open_messages, "TEXT_MESSAGE_END sem START"
            open_messages.remove(event.message_id)
            closed_messages.add(event.message_id)
        elif isinstance(event, ToolCallStartEvent):
            assert event.tool_call_id not in tool_state, "tool_call_id repetido"
            tool_state[event.tool_call_id] = "started"
        elif isinstance(event, ToolCallArgsEvent):
            assert tool_state.get(event.tool_call_id) in ("started", "args"), "ARGS fora de uma tool call aberta"
            tool_state[event.tool_call_id] = "args"
        elif isinstance(event, ToolCallEndEvent):
            assert tool_state.get(event.tool_call_id) in ("started", "args"), "TOOL_CALL_END sem START"
            tool_state[event.tool_call_id] = "ended"
        elif isinstance(event, ToolCallResultEvent):
            assert tool_state.get(event.tool_call_id) == "ended", "RESULT antes do END da tool call"
            tool_state[event.tool_call_id] = "result"
    assert not open_messages, f"mensagens sem TEXT_MESSAGE_END: {open_messages}"
    assert all(s in ("ended", "result") for s in tool_state.values()), f"tool calls abertas: {tool_state}"


def types_of(events: list[BaseEvent]) -> list[str]:
    return [e.type.value for e in events]


def text_of(events: list[BaseEvent]) -> str:
    return "".join(e.delta for e in events if isinstance(e, TextMessageContentEvent))
