"""Rotas AG-UI por entidade: ``POST /agui/{entity_id}`` para cada Agent e Team (F1-08).

Substitui a interface ``AGUI`` do agno 2.5.8 (``agno/os/interfaces/agui/router.py``), que
fixa a rota em ``/agui`` (com várias entidades só a primeira respondia; o AgentOS descarta
as repetidas), põe ``Access-Control-Allow-Origin: *`` na resposta SSE e deixa o erro do run
sumir (o conversor do agno não mapeia ``RunError``). Aqui:

- a entidade sai de um registro montado no startup (agentes antes de teams; id
  inexistente = 404). ``POST /agui`` continua como alias **deprecated** da primeira
  entidade, com ``Deprecation: true`` e ``Link`` para a rota nova;
- a resposta SSE não leva header CORS: quem decide é o ``CORSMiddleware`` do app;
- falha do run (``RunError``/``TeamRunError`` ou ``RunCancelled`` do próprio run, ou
  exceção) vira ``RUN_ERROR`` no lugar do ``RUN_FINISHED``, com mensagem curta: a de um
  guardrail (texto escrito por quem o configurou) ou uma genérica; o texto de exceção
  (que pode carregar segredo de SDK) nunca vai ao cliente; este módulo loga só o tipo (o
  agno loga o texto do erro do run);
- ``runId`` do cliente só vale em ``[A-Za-z0-9_-]{1,64}`` e se não for o de um run em
  andamento (F1-10); senão, o servidor gera um UUID4 e devolve no ``RUN_STARTED``;
- ``GET /status`` da interface do agno é mantido (``{"status": "available"}``).

A conversão de mensagens e de eventos é a do próprio agno
(``agno/os/interfaces/agui/utils.py``). ``user_id`` continua vindo de
``forwardedProps.user_id``; só passa se for ``str`` (outro tipo vira ``None``: um
``{"$ne": null}`` chegaria ao filtro do ``MongoDb`` do agno). O resto da validade é do
guardrail do F1-06 em entidade com memória de usuário; amarrar à credencial é o F5-02.
"""

from __future__ import annotations

import re
import uuid
from collections.abc import AsyncIterator, Sequence
from typing import cast
from urllib.parse import quote

from ag_ui.core import BaseEvent, EventType, RunAgentInput, RunErrorEvent, RunStartedEvent
from ag_ui.encoder import EventEncoder
from agno.agent import Agent
from agno.os.interfaces.agui.utils import (
    async_stream_agno_response_as_agui_events,
    convert_agui_messages_to_agno_messages,
    validate_agui_state,
)
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

AGUI_PATH = "/agui"
_RUN_ID_PATTERN = re.compile(r"[A-Za-z0-9_-]{1,64}")
# Erros de guardrail (InputCheckError/OutputCheckError do agno, agno/exceptions.py): o texto
# é o que o guardrail escreveu, não uma exceção de SDK.
_CHECK_ERROR_TYPES = frozenset({"input_check_error", "output_check_error"})
_MAX_ERROR_MESSAGE = 300
_GENERIC_ERROR = "Falha ao executar o run; detalhes no log do servidor."
_CANCELLED = "Run cancelado."

Entity = Agent | Team
AgnoEvent = RunOutputEvent | TeamRunOutputEvent
_FAILURE_EVENTS = (AgentRunErrorEvent, TeamRunErrorEvent, AgentRunCancelledEvent, TeamRunCancelledEvent)


def resolve_run_id(candidate: str) -> str:
    """``runId`` do cliente se for curto e de charset seguro; senão, um UUID4 novo."""
    if _RUN_ID_PATTERN.fullmatch(candidate):
        return candidate
    return str(uuid.uuid4())


def build_agui_router(agents: Sequence[Agent], teams: Sequence[Team], logger: ILogger) -> APIRouter:
    """Router com ``POST /agui/{entity_id}`` e o alias deprecated ``POST /agui``."""
    entities = _register(agents, teams, logger)
    router = APIRouter(tags=["AGUI"])
    encoder = EventEncoder()

    def stream(entity_id: str, entity: Entity, run_input: RunAgentInput) -> StreamingResponse:
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

        async def body() -> AsyncIterator[str]:
            async for event in _run_events(entity_id, entity, run_input, run_id, logger):
                yield encoder.encode(event)

        return StreamingResponse(body(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    @router.post(AGUI_PATH + "/{entity_id}", name="agui_run_entity")
    async def run_entity(entity_id: str, run_input: RunAgentInput) -> StreamingResponse:
        entity = entities.get(entity_id)
        if entity is None:
            raise HTTPException(status_code=404, detail="entidade não encontrada")
        return stream(entity_id, entity, run_input)

    if entities:
        default_id = next(iter(entities))
        # Id vem da config (pode ter caractere fora de latin-1): no header vai como URI.
        successor_link = f'<{AGUI_PATH}/{quote(default_id, safe="")}>; rel="successor-version"'
        logger.info(
            "AG-UI montado por entidade; POST /agui é alias deprecated da primeira",
            entity_ids=list(entities),
            default_entity_id=default_id,
        )

        @router.post(AGUI_PATH, name="agui_run_default", deprecated=True)
        async def run_default(run_input: RunAgentInput) -> StreamingResponse:
            response = stream(default_id, entities[default_id], run_input)
            response.headers["Deprecation"] = "true"
            response.headers["Link"] = successor_link
            return response

    @router.get("/status", name="agui_status")
    async def status() -> dict[str, str]:
        """Mesmo contrato do ``GET /status`` da interface AGUI do agno (o frontend o consulta)."""
        return {"status": "available"}

    return router


def _register(agents: Sequence[Agent], teams: Sequence[Team], logger: ILogger) -> dict[str, Entity]:
    """Registro id -> entidade, agentes antes de teams; id repetido fica com o primeiro."""
    entities: dict[str, Entity] = {}
    candidates: list[Entity] = [*agents, *teams]
    for entity in candidates:
        entity_id = entity.id
        if not isinstance(entity_id, str) or not entity_id:
            logger.warning("Entidade sem id não exposta no AG-UI", entity_name=str(entity.name))
            continue
        if entity_id in entities:
            logger.warning("Id de entidade repetido; AG-UI expõe só a primeira", entity_id=entity_id)
            continue
        entities[entity_id] = entity
    return entities


async def _run_events(
    entity_id: str, entity: Entity, run_input: RunAgentInput, run_id: str, logger: ILogger
) -> AsyncIterator[BaseEvent]:
    thread_id = run_input.thread_id
    yield RunStartedEvent(type=EventType.RUN_STARTED, thread_id=thread_id, run_id=run_id)
    failures: list[AgnoEvent] = []
    try:
        user_id = _forwarded_user_id(run_input, entity_id, run_id, logger)
        stream = _start_run(entity, run_input, run_id, user_id)
        async for event in async_stream_agno_response_as_agui_events(
            response_stream=_watch_failure(stream, run_id, failures), thread_id=thread_id, run_id=run_id
        ):
            if event.type == EventType.RUN_FINISHED and failures:
                yield _failure_event(failures[0], entity_id, run_id, logger)
                return
            yield event
    except Exception as exc:  # noqa: BLE001 - o SSE já respondeu 200: a falha vira RUN_ERROR, com log
        # Nunca str(exc): texto de exceção de SDK pode trazer segredo.
        logger.error("Falha no run AG-UI", entity_id=entity_id, run_id=run_id, error_type=type(exc).__name__)
        yield RunErrorEvent(type=EventType.RUN_ERROR, message=_GENERIC_ERROR, code="run_error")


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
    """Mesmos argumentos do router do agno 2.5.8 (``stream_steps`` do team era ignorado)."""
    messages = convert_agui_messages_to_agno_messages(run_input.messages or [])
    session_state = validate_agui_state(run_input.state, run_input.thread_id)
    if isinstance(entity, Team):
        return entity.arun(
            input=messages,
            session_id=run_input.thread_id,
            stream=True,
            user_id=user_id,
            session_state=session_state,
            run_id=run_id,
        )
    agent_stream = entity.arun(
        input=messages,
        session_id=run_input.thread_id,
        stream=True,
        stream_events=True,
        user_id=user_id,
        session_state=session_state,
        run_id=run_id,
    )
    # O overload do agno inclui RunOutput no stream, que só aparece com yield_run_output=True.
    return cast(AsyncIterator[AgnoEvent], agent_stream)


async def _watch_failure(
    stream: AsyncIterator[AgnoEvent], run_id: str, failures: list[AgnoEvent]
) -> AsyncIterator[AgnoEvent]:
    """Repassa o stream e guarda a falha do próprio run (a de um membro tem outro run_id)."""
    async for chunk in stream:
        if isinstance(chunk, _FAILURE_EVENTS) and chunk.run_id == run_id:
            failures.append(chunk)
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
