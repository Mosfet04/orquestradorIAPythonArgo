"""Rotas AG-UI por entidade: ``POST /agui/{entity_id}`` para cada Agent e Team (F1-08).

Substitui a interface ``AGUI`` do agno 2.5.8 (``agno/os/interfaces/agui/router.py``), que
fixa a rota em ``/agui`` (com várias entidades só a primeira respondia; o AgentOS descarta
as repetidas), põe ``Access-Control-Allow-Origin: *`` na resposta SSE e deixa o erro do run
sumir (o conversor do agno não mapeia ``RunError``). Aqui:

- a entidade sai, a cada request, do registro das entidades servidas (``ServedEntities``,
  trocado a quente pelo FA-04; agentes antes de teams; id inexistente = 404). ``POST /agui``
  continua como alias **deprecated** da primeira entidade servida, com ``Deprecation: true`` e
  ``Link`` para a rota nova (sem nenhuma, 404);
- a resposta SSE não leva header CORS: quem decide é o ``CORSMiddleware`` do app;
- falha do run (``RunError``/``TeamRunError`` ou ``RunCancelled`` do próprio run, ou
  exceção) vira ``RUN_ERROR`` no lugar do ``RUN_FINISHED``, com mensagem curta: a de um
  guardrail (texto escrito por quem o configurou) ou uma genérica; o texto de exceção
  (que pode carregar segredo de SDK) nunca vai ao cliente; este módulo loga só o tipo (o
  agno loga o texto do erro do run);
- ``runId`` do cliente só vale em ``[A-Za-z0-9_-]{1,64}`` e se não for o de um run em
  andamento (F1-10) nem o de um run já gravado (F3-05: no agno 3.x a chave do run gravado é o
  ``run_id``); senão, o servidor gera um UUID4 e devolve no ``RUN_STARTED``;
- ``GET /status`` da interface do agno é mantido (``{"status": "available"}``);
- ``threadId`` de uma sessão que já é de outro ``user_id`` é recusado com 404 antes do stream
  (``assert_session_writable`` do agno 3.x, o mesmo do AG-UI e das rotas REST dele, F3-05): no
  3.x os runs vão para ``agno_runs`` sem o predicado de dono do ``upsert_session`` e o turno de
  quem pediu entraria no histórico do dono.

Entrada e saída neutras à versão do agno (F3-04):

- a entrada é nossa: o run recebe só o texto da última mensagem ``user`` (partes de texto juntadas
  por quebra de linha; sem ela, texto vazio) e o ``forwardedProps.user_id``. Histórico, ``state``,
  ``tools`` e ``context`` do cliente e mídia ficam de fora: o histórico vem da sessão
  (``threadId``), como no AG-UI do agno 3.x, e o cliente não injeta mensagem de assistente,
  estado de sessão, tool para o modelo chamar nem URL para o servidor baixar;
- a saída usa o conversor de eventos do agno (``agno/os/interfaces/agui/stream.py`` no 3.x) e
  passa por ``filter_agui_events``: só os tipos de ``EMITTED_EVENT_TYPES`` chegam ao cliente
  (``RAW``, ``CUSTOM``, estado e raciocínio do 3.x são descartados, com log ``debug`` só do tipo:
  nenhum ``CUSTOM`` é previsto hoje) e o terminal é decidido aqui (o ``RUN_ERROR`` do conversor
  do 3.x leva o texto do erro e o evento cru);
- agente com ``expose_reasoning=false`` (F3-10): os ``TOOL_CALL_*`` das tools de raciocínio dele
  (``think``/``analyze`` da ``PrivateReasoningTools``, reconhecidas pela origem; em team, pelo membro que
  chamou) também são descartados (``reasoning_visibility``). O run e a sessão seguem com o raciocínio.

``user_id`` só passa se for ``str`` (outro tipo vira ``None``: um ``{"$ne": null}`` chegaria ao
filtro do ``MongoDb`` do agno). O resto da validade é do guardrail do F1-06 em entidade com
memória de usuário; amarrar à credencial é o F5-02.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Container, Sequence
from typing import cast
from urllib.parse import quote

from ag_ui.core import (
    BaseEvent,
    EventType,
    Message,
    RunAgentInput,
    RunErrorEvent,
    RunStartedEvent,
    TextInputContent,
    UserMessage,
)
from ag_ui.encoder import EventEncoder
from agno.db.base import AsyncBaseDb
from agno.os.interfaces.agui.handlers import is_completion_event
from agno.os.interfaces.agui.stream import async_stream_agno_response_as_agui_events
from agno.os.middleware.user_scope import assert_session_writable
from agno.run.agent import RunCancelledEvent as AgentRunCancelledEvent
from agno.run.agent import RunErrorEvent as AgentRunErrorEvent
from agno.run.agent import RunOutputEvent
from agno.run.cancel import get_cancellation_manager
from agno.run.team import RunCancelledEvent as TeamRunCancelledEvent
from agno.run.team import RunErrorEvent as TeamRunErrorEvent
from agno.run.team import TeamRunOutputEvent
from agno.team import Team
from fastapi import APIRouter, HTTPException
from fastapi.responses import StreamingResponse

from src.domain.ports import ILogger
from src.infrastructure.runtime.agno.reasoning_visibility import (
    private_reasoning_tools,
    track_private_reasoning_calls,
)
from src.infrastructure.runtime.agno.served_entities import Entity, ServedEntities

AGUI_PATH = "/agui"
_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
# Erros de guardrail (InputCheckError/OutputCheckError do agno, agno/exceptions.py): o texto
# é o que o guardrail escreveu, não uma exceção de SDK.
_CHECK_ERROR_TYPES = frozenset({"input_check_error", "output_check_error"})
_MAX_ERROR_MESSAGE = 300
_GENERIC_ERROR = "Falha ao executar o run; detalhes no log do servidor."
_CANCELLED = "Run cancelado."
_ENTITY_NOT_FOUND = "entidade não encontrada"

AgnoEvent = RunOutputEvent | TeamRunOutputEvent
_FAILURE_EVENTS = (AgentRunErrorEvent, TeamRunErrorEvent, AgentRunCancelledEvent, TeamRunCancelledEvent)

_TERMINAL_EVENT_TYPES = frozenset({EventType.RUN_FINISHED, EventType.RUN_ERROR})
_TOOL_CALL_EVENT_TYPES = frozenset(
    {EventType.TOOL_CALL_START, EventType.TOOL_CALL_ARGS, EventType.TOOL_CALL_END, EventType.TOOL_CALL_RESULT}
)
# Do conversor do agno, passam sem mudança (o RUN_STARTED é nosso; os terminais são decididos aqui).
_PASSTHROUGH_EVENT_TYPES = frozenset(
    {
        EventType.TEXT_MESSAGE_START,
        EventType.TEXT_MESSAGE_CONTENT,
        EventType.TEXT_MESSAGE_END,
        EventType.TOOL_CALL_START,
        EventType.TOOL_CALL_ARGS,
        EventType.TOOL_CALL_END,
        EventType.TOOL_CALL_RESULT,
        # Raciocínio do agno 2.5.8 (``reasoning_started``/``reasoning_completed``), sem conteúdo.
        EventType.STEP_STARTED,
        EventType.STEP_FINISHED,
    }
)
EMITTED_EVENT_TYPES = _PASSTHROUGH_EVENT_TYPES | _TERMINAL_EVENT_TYPES | {EventType.RUN_STARTED}
"""Todos os tipos de evento que o AG-UI deste app emite."""


def resolve_run_id(candidate: str) -> str:
    """``runId`` do cliente se for curto e de charset seguro; senão, um UUID4 novo."""
    if _RUN_ID_PATTERN.fullmatch(candidate):
        return candidate
    return str(uuid.uuid4())


def build_agui_router(served: ServedEntities, logger: ILogger) -> APIRouter:
    """Router com ``POST /agui/{entity_id}`` e o alias deprecated ``POST /agui``, sobre as entidades servidas."""
    router = APIRouter(tags=["AGUI"])
    encoder = EventEncoder()

    async def stream(entity_id: str, entity: Entity, run_input: RunAgentInput) -> StreamingResponse:
        run_id = resolve_run_id(run_input.run_id)
        if run_id != run_input.run_id:
            logger.info(
                "runId do cliente vazio ou fora do formato; gerado pelo servidor",
                entity_id=entity_id,
                run_id=run_id,
                client_run_id_length=len(run_input.run_id),
            )
        elif run_id in get_cancellation_manager().get_active_runs():
            # Mesmo run_id de um run em andamento dividiria a entrada do gerenciador de
            # cancelamento: o fim de um apagaria o registro do outro e um cancel atingiria os
            # dois (F1-10). Não fecha a corrida de dois pedidos simultâneos com o mesmo id.
            run_id = str(uuid.uuid4())
            logger.info(
                "runId do cliente já em uso por um run em andamento; gerado pelo servidor",
                entity_id=entity_id,
                run_id=run_id,
            )
        user_id = _forwarded_user_id(run_input, entity_id, run_id, logger)
        try:
            # Antes do stream, para a recusa ser um 404 de verdade. Sem bypass de admin: a chave da
            # API não é uma identidade (F5). Falha ao ler sobe (500): sem conferir o dono, não roda.
            db = entity.db
            await assert_session_writable(db, run_input.thread_id, user_id or entity.user_id)
            stored = isinstance(db, AsyncBaseDb) and await db.get_run(run_id, deserialize=False) is not None
        except HTTPException:
            raise
        except Exception as exc:
            # Nunca str(exc): o texto do driver pode trazer a URI do Mongo.
            logger.error(
                "Falha ao conferir a sessão e o runId do AG-UI",
                entity_id=entity_id,
                run_id=run_id,
                error_type=type(exc).__name__,
            )
            raise
        if stored:
            # O run gravado não é regravado por um run novo: o de outro dono seria recusado na
            # gravação (``SessionDb.upsert_run``) e este run ficaria fora do histórico; o do próprio
            # dono seria trocado. Não vê o run só do campo legado ``runs`` da sessão (F3-06).
            run_id = str(uuid.uuid4())
            logger.info("runId do cliente já gravado; gerado pelo servidor", entity_id=entity_id, run_id=run_id)

        async def body() -> AsyncIterator[str]:
            async for event in _run_events(entity_id, entity, run_input, run_id, user_id, logger):
                yield encoder.encode(event)

        return StreamingResponse(body(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @router.post(AGUI_PATH + "/{entity_id}", name="agui_run_entity")
    async def run_entity(entity_id: str, run_input: RunAgentInput) -> StreamingResponse:
        entity = served.by_id.get(entity_id)
        if entity is None:
            raise HTTPException(status_code=404, detail=_ENTITY_NOT_FOUND)
        return await stream(entity_id, entity, run_input)

    @router.post(AGUI_PATH, name="agui_run_default", deprecated=True)
    async def run_default(run_input: RunAgentInput) -> StreamingResponse:
        default = next(iter(served.by_id.items()), None)
        if default is None:
            raise HTTPException(status_code=404, detail=_ENTITY_NOT_FOUND)
        default_id, entity = default
        response = await stream(default_id, entity, run_input)
        response.headers["Deprecation"] = "true"
        # Id vem da config (pode ter caractere fora de latin-1): no header vai como URI.
        response.headers["Link"] = f'<{AGUI_PATH}/{quote(default_id, safe="")}>; rel="successor-version"'
        return response

    @router.get("/status", name="agui_status")
    async def status() -> dict[str, str]:
        """Mesmo contrato do ``GET /status`` da interface AGUI do agno (o frontend o consulta)."""
        return {"status": "available"}

    return router


async def _run_events(
    entity_id: str, entity: Entity, run_input: RunAgentInput, run_id: str, user_id: str | None, logger: ILogger
) -> AsyncIterator[BaseEvent]:
    thread_id = run_input.thread_id
    yield RunStartedEvent(type=EventType.RUN_STARTED, thread_id=thread_id, run_id=run_id)
    failures: list[AgnoEvent] = []
    hidden_ids: set[str] = set()
    try:
        stream = track_private_reasoning_calls(
            _start_run(entity, run_input, run_id, user_id), private_reasoning_tools(entity), hidden_ids
        )
        converted = async_stream_agno_response_as_agui_events(
            response_stream=_watch_failure(stream, run_id, failures), thread_id=thread_id, run_id=run_id
        )
        async for event in filter_agui_events(
            converted, failures, entity_id=entity_id, run_id=run_id, logger=logger, hidden_tool_call_ids=hidden_ids
        ):
            yield event
    except Exception as exc:  # noqa: BLE001 - o SSE já respondeu 200: a falha vira RUN_ERROR, com log
        # Nunca str(exc): texto de exceção de SDK pode trazer segredo.
        logger.error("Falha no run AG-UI", entity_id=entity_id, run_id=run_id, error_type=type(exc).__name__)
        yield RunErrorEvent(type=EventType.RUN_ERROR, message=_GENERIC_ERROR, code="run_error")


async def filter_agui_events(
    events: AsyncIterator[BaseEvent],
    failures: Sequence[AgnoEvent],
    *,
    entity_id: str,
    run_id: str,
    logger: ILogger,
    hidden_tool_call_ids: Container[str] = frozenset(),
) -> AsyncIterator[BaseEvent]:
    """Saída do conversor do agno reduzida à lista: o resto é descartado e o terminal é decidido aqui.

    ``failures`` é a lista que o ``_watch_failure`` preenche enquanto o stream é consumido: no
    terminal, já tem a falha do próprio run, se houve. Nada passa depois do terminal.
    ``hidden_tool_call_ids``: ids das chamadas de raciocínio privado (F3-10), preenchidos pelo
    ``track_private_reasoning_calls`` antes de o conversor emitir os ``TOOL_CALL_*`` delas.
    """
    async for event in events:
        if event.type in _TERMINAL_EVENT_TYPES:
            yield _terminal_event(event, failures, entity_id, run_id, logger)
            return
        if event.type in _TOOL_CALL_EVENT_TYPES and getattr(event, "tool_call_id", None) in hidden_tool_call_ids:
            logger.debug(
                "Raciocínio do agente fora do AG-UI (expose_reasoning=false)",
                entity_id=entity_id,
                run_id=run_id,
                event_type=event.type.value,
            )
            continue
        if event.type in _PASSTHROUGH_EVENT_TYPES:
            yield event
            continue
        logger.debug(
            "Evento AG-UI fora da lista descartado", entity_id=entity_id, run_id=run_id, event_type=event.type.value
        )


def _terminal_event(
    event: BaseEvent, failures: Sequence[AgnoEvent], entity_id: str, run_id: str, logger: ILogger
) -> BaseEvent:
    """Falha do próprio run vence; ``RUN_ERROR`` do conversor (texto do erro) vira o genérico."""
    if failures:
        return _failure_event(failures[0], entity_id, run_id, logger)
    if event.type == EventType.RUN_ERROR:
        logger.warning("Run AG-UI terminou sem sucesso", entity_id=entity_id, run_id=run_id, code="run_error")
        return RunErrorEvent(type=EventType.RUN_ERROR, message=_GENERIC_ERROR, code="run_error")
    return event


def last_user_text(messages: Sequence[Message]) -> str:
    """Texto da última mensagem ``user`` (partes de texto juntadas por quebra de linha); sem ela, ``""``.

    Partes de mídia ficam de fora (o servidor não baixa URL nem decodifica arquivo do cliente).
    """
    for message in reversed(messages):
        if isinstance(message, UserMessage):
            if isinstance(message.content, str):
                return message.content
            return "\n".join(part.text for part in message.content if isinstance(part, TextInputContent))
    return ""


def _forwarded_user_id(run_input: RunAgentInput, entity_id: str, run_id: str, logger: ILogger) -> str | None:
    """``forwardedProps.user_id`` se for ``str``; outro tipo vira ``None`` (log só com o tipo)."""
    props = run_input.forwarded_props
    raw = props.get("user_id") if isinstance(props, dict) else None
    if raw is None or isinstance(raw, str):
        return raw
    logger.info(
        "forwardedProps.user_id não é string; run segue sem user_id",
        entity_id=entity_id,
        run_id=run_id,
        user_id_type=type(raw).__name__,
    )
    return None


def _start_run(
    entity: Entity, run_input: RunAgentInput, run_id: str, user_id: str | None
) -> AsyncIterator[AgnoEvent]:
    """Run com o texto da última mensagem do usuário; o histórico é o da sessão ``thread_id``.

    Sem ``session_state`` do cliente (era o ``state`` do AG-UI até o F3-04). O team não recebe
    ``stream_events`` (o router do agno 2.5.8 também não passava).
    """
    text = last_user_text(run_input.messages or [])
    if isinstance(entity, Team):
        return entity.arun(
            input=text, session_id=run_input.thread_id, stream=True, user_id=user_id, run_id=run_id
        )
    agent_stream = entity.arun(
        input=text,
        session_id=run_input.thread_id,
        stream=True,
        stream_events=True,
        user_id=user_id,
        run_id=run_id,
    )
    # O overload do agno inclui RunOutput no stream, que só aparece com yield_run_output=True.
    return cast(AsyncIterator[AgnoEvent], agent_stream)


async def _watch_failure(
    stream: AsyncIterator[AgnoEvent], run_id: str, failures: list[AgnoEvent]
) -> AsyncIterator[AgnoEvent]:
    """Repassa o stream e guarda a falha do próprio run (a de um membro tem outro run_id).

    O terminal de um membro (``RunCompleted``/``RunError``/``RunPaused`` com outro run_id) não passa:
    o team do agno 3.x repassa os eventos do membro e o conversor tomaria o último terminal como o
    do run (erro de um membro viraria ``RUN_ERROR`` do team que respondeu).
    """
    async for chunk in stream:
        if chunk.run_id == run_id and isinstance(chunk, _FAILURE_EVENTS):
            failures.append(chunk)
            continue
        if chunk.run_id != run_id and is_completion_event(chunk):
            continue
        yield chunk


def _failure_event(chunk: AgnoEvent, entity_id: str, run_id: str, logger: ILogger) -> RunErrorEvent:
    if isinstance(chunk, (AgentRunCancelledEvent, TeamRunCancelledEvent)):
        message, code = _CANCELLED, "run_cancelled"
    elif (
        isinstance(chunk, (AgentRunErrorEvent, TeamRunErrorEvent))
        and chunk.error_type in _CHECK_ERROR_TYPES
        and isinstance(chunk.content, str)
        and chunk.content
    ):
        message, code = chunk.content[:_MAX_ERROR_MESSAGE], chunk.error_type
    else:
        message, code = _GENERIC_ERROR, "run_error"
    logger.warning("Run AG-UI terminou sem sucesso", entity_id=entity_id, run_id=run_id, code=code)
    return RunErrorEvent(type=EventType.RUN_ERROR, message=message, code=code)
