"""Modelo de chat e embedder falsos, determinísticos e sem rede.

``FakeChatModel`` herda de ``agno.models.base.Model`` (agno 2.5.8) para poder ser
passado direto a ``Agent``/``Team`` do agno; ``FakeEmbedder`` herda de
``agno.knowledge.embedder.base.Embedder`` pelo mesmo motivo. Quando o runtime
deixar de ser o agno (F2), estes fakes acompanham o adapter.
"""

from __future__ import annotations

import asyncio
import copy
import hashlib
import math
import struct
from collections.abc import AsyncIterator, Iterator
from dataclasses import dataclass, field
from typing import Any

from agno.knowledge.embedder.base import Embedder
from agno.models.base import Model
from agno.models.response import ModelResponse

from src.domain.ports import IEmbedderFactory, IModelFactory


def running_on_event_loop() -> bool:
    """``True`` se a thread atual está rodando um event loop asyncio.

    Detector simples de I/O síncrono no caminho async: chamada bloqueante feita via
    ``asyncio.to_thread`` roda numa thread sem loop e devolve ``False``.
    """
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


class ScriptExhaustedError(AssertionError):
    """O teste pediu mais respostas do que o roteiro do ``FakeChatModel`` tinha."""


@dataclass(frozen=True)
class FakeModelCall:
    """Uma chamada recebida pelo ``FakeChatModel``."""

    messages: tuple[tuple[str, str], ...]
    tool_names: tuple[str, ...] = ()
    tools: tuple[Any, ...] = ()
    """Tools como o agno as entrega ao provider (``{"type": "function", "function": {...}}``)."""

    @property
    def last_user_message(self) -> str | None:
        for role, content in reversed(self.messages):
            if role == "user":
                return content
        return None


def _message_text(message: Any) -> str:
    getter = getattr(message, "get_content_string", None)
    if callable(getter):
        return str(getter())
    return str(getattr(message, "content", message))


def _tool_name(tool: Any) -> str:
    if isinstance(tool, dict):
        fn = tool.get("function", tool)
        return str(fn.get("name", ""))
    return str(getattr(tool, "name", tool))


@dataclass
class FakeChatModel(Model):
    """Modelo de chat roteirizado: devolve ``responses`` em ordem e registra cada chamada.

    Item ``str`` vira resposta de texto do assistente; item ``ModelResponse`` é devolvido
    como está (ex.: ``ModelResponse(role="assistant", tool_calls=[...])`` para o agno
    executar uma tool e chamar o modelo de novo com o resultado).

    Segue o contrato dos providers do agno 2.5.8 (``invoke(messages: List[Message], ...)``,
    ex.: ``agno/models/openai/chat.py``): prompt solto (``invoke("texto")``) levanta
    ``TypeError`` como no provider real, em vez de mascarar a chamada errada (F1-07, B8).
    Use ``await model.aresponse(messages=[Message(...)])`` ou ``Agent``/``Team``. Toda
    chamada válida é registrada em ``calls`` (``messages=()`` quando não há mensagens)
    antes de escolher a resposta, então a chamada nº N recebe sempre ``responses[N-1]``.

    Roteiro esgotado levanta ``ScriptExhaustedError``. Chamado direto, o erro sobe para o
    teste; dentro de ``Agent.run``/``Agent.arun`` (e ``Team``) o agno captura a exceção e
    devolve ``RunOutput`` com ``status == RunStatus.error``. Nesse caso, asserte
    ``model.exhausted`` (ou o ``status``) em vez de esperar a exceção.
    """

    id: str = "fake-chat-model"
    name: str | None = "FakeChatModel"
    provider: str | None = "fake"
    responses: list[str | ModelResponse] = field(default_factory=list)
    calls: list[FakeModelCall] = field(default_factory=list)

    @property
    def exhausted(self) -> bool:
        """``True`` se alguma chamada pediu resposta além do roteiro."""
        return len(self.calls) > len(self.responses)

    # ── roteiro ─────────────────────────────────────────────────────

    @staticmethod
    def _as_call(args: tuple[Any, ...], kwargs: dict[str, Any]) -> FakeModelCall:
        messages = kwargs.get("messages")
        if messages is None and args:
            messages = args[0]
        if messages is not None and not isinstance(messages, list):
            raise TypeError(
                f"FakeChatModel: messages deve ser List[Message] como nos providers do agno, "
                f"recebeu {type(messages).__name__}"
            )
        tools = tuple(copy.deepcopy(t) if isinstance(t, dict) else t for t in kwargs.get("tools") or ())
        return FakeModelCall(
            messages=tuple((str(m.role), _message_text(m)) for m in messages or ()),
            tool_names=tuple(_tool_name(t) for t in tools),
            tools=tools,
        )

    def _next_response(self, *args: Any, **kwargs: Any) -> ModelResponse:
        self.calls.append(self._as_call(args, kwargs))
        index = len(self.calls) - 1
        if index >= len(self.responses):
            raise ScriptExhaustedError(
                f"FakeChatModel: roteiro esgotado ({len(self.responses)} resposta(s), chamada nº {index + 1})"
            )
        scripted = self.responses[index]
        if isinstance(scripted, ModelResponse):
            return scripted
        return ModelResponse(role="assistant", content=scripted)

    # ── contrato de agno.models.base.Model ──────────────────────────

    def invoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        return self._next_response(*args, **kwargs)

    async def ainvoke(self, *args: Any, **kwargs: Any) -> ModelResponse:
        return self._next_response(*args, **kwargs)

    def invoke_stream(self, *args: Any, **kwargs: Any) -> Iterator[ModelResponse]:
        yield self._next_response(*args, **kwargs)

    async def ainvoke_stream(self, *args: Any, **kwargs: Any) -> AsyncIterator[ModelResponse]:
        yield self._next_response(*args, **kwargs)

    def _parse_provider_response(self, response: Any, **kwargs: Any) -> ModelResponse:
        return response if isinstance(response, ModelResponse) else ModelResponse(content=str(response))

    def _parse_provider_response_delta(self, response: Any) -> ModelResponse:
        return self._parse_provider_response(response)


@dataclass
class FakeEmbedder(Embedder):
    """Embedder determinístico: o mesmo texto gera sempre o mesmo vetor unitário."""

    id: str = "fake-embedder"
    provider: str = "fake"
    dimensions: int | None = 32
    calls: list[str] = field(default_factory=list)
    on_event_loop: list[bool] = field(default_factory=list)
    """Por chamada de ``get_embedding``: ``True`` se rodou na thread do event loop (bloqueante)."""

    def get_embedding(self, text: str) -> list[float]:
        self.calls.append(text)
        self.on_event_loop.append(running_on_event_loop())
        size = self.dimensions or 32
        raw: list[float] = []
        counter = 0
        while len(raw) < size:
            digest = hashlib.sha256(f"{counter}:{text}".encode()).digest()
            raw.extend(v / 2**31 for (v,) in struct.iter_unpack(">i", digest))
            counter += 1
        vec = raw[:size]
        norm = math.sqrt(sum(x * x for x in vec)) or 1.0
        return [x / norm for x in vec]

    def get_embedding_and_usage(self, text: str) -> tuple[list[float], dict[str, Any] | None]:
        return self.get_embedding(text), None

    async def async_get_embedding(self, text: str) -> list[float]:
        return self.get_embedding(text)

    async def async_get_embedding_and_usage(self, text: str) -> tuple[list[float], dict[str, Any] | None]:
        return self.get_embedding_and_usage(text)


FactoryCall = tuple[str, str, dict[str, Any]]
"""``(provider, model_id, kwargs)`` recebidos por ``create_model`` de uma fábrica fake."""


class FakeModelFactory(IModelFactory):
    """``IModelFactory`` que devolve ``FakeChatModel`` com o ``id``/provider pedidos."""

    def __init__(self, *, responses: list[str] | None = None, invalid_models: set[str] | None = None) -> None:
        self._responses = list(responses or [])
        self._invalid = set(invalid_models or ())
        self.created: list[FactoryCall] = []
        self.models: list[FakeChatModel] = []
        """Instâncias devolvidas por ``create_model``, na ordem (para assertar ``calls``)."""

    def create_model(self, factory_ia_model: str, model_id: str, **kwargs: Any) -> FakeChatModel:
        self.created.append((factory_ia_model, model_id, dict(kwargs)))
        model = FakeChatModel(id=model_id, provider=factory_ia_model, responses=list(self._responses))
        self.models.append(model)
        return model

    def validate_model_config(self, factory_ia_model: str, model_id: str) -> dict[str, Any]:
        errors = [f"modelo {model_id!r} marcado como inválido no fake"] if model_id in self._invalid else []
        return {"valid": not errors, "factory_type": factory_ia_model, "model_id": model_id, "errors": errors}


class FakeEmbedderFactory(IEmbedderFactory):
    """``IEmbedderFactory`` que devolve ``FakeEmbedder`` identificado pelo ``id``/provider pedidos."""

    def __init__(self, *, dimensions: int = 32) -> None:
        self._dimensions = dimensions
        self.created: list[FactoryCall] = []
        self.embedders: list[FakeEmbedder] = []
        """Instâncias devolvidas por ``create_model``, na ordem (para assertar ``calls``/``on_event_loop``)."""

    def create_model(self, factory_ia_model: str, model_id: str, **kwargs: Any) -> FakeEmbedder:
        self.created.append((factory_ia_model, model_id, dict(kwargs)))
        embedder = FakeEmbedder(id=model_id, provider=factory_ia_model, dimensions=self._dimensions)
        self.embedders.append(embedder)
        return embedder
