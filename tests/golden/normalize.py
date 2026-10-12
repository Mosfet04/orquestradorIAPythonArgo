"""Converte os kwargs passados a ``Agent``/``Team`` do agno numa forma JSON estável.

Objetos não serializáveis viram ``{"__type__": <módulo.Classe>, ...atributos relevantes}``:
o suficiente para o golden pegar regressão real (modelo trocado, coleção errada, tool
sumindo, parâmetro de tool mudando), sem depender de ``id()``/endereço de memória.
Tipo desconhecido é erro: quem introduzir um kwarg novo decide como ele é comparado.

Os fakes de ``tests/fakes`` aparecem como ``"__type__": "fake"`` (independe do módulo onde
moram) e URLs de conexão saem sem ``user:senha@``.
"""

from __future__ import annotations

import re
from collections.abc import Callable
from enum import Enum
from inspect import iscoroutinefunction
from typing import Any

from agno.agent import Agent
from agno.db.base import AsyncBaseDb, BaseDb
from agno.guardrails.base import BaseGuardrail
from agno.knowledge import Knowledge
from agno.knowledge.embedder.base import Embedder
from agno.models.base import Model
from agno.team import Team
from agno.tools import Toolkit
from agno.tools.function import Function
from agno.vectordb.base import VectorDb

from tests.fakes import FakeChatModel, FakeEmbedder

JsonValue = None | bool | int | float | str | list["JsonValue"] | dict[str, "JsonValue"]

_FAKE_TYPES = (FakeChatModel, FakeEmbedder)
# Regra do pymongo (uri_parser_shared.parse_uri): autoridade = tudo antes do primeiro "/" ou "?";
# userinfo = tudo até o último "@" dela. "#" não corta (pymongo aceita na senha); mascarar a mais é seguro.
_URL_USERINFO = re.compile(r"^(?P<scheme>[A-Za-z][A-Za-z0-9+.-]*://)[^/?]*@")


def _type_name(obj: object) -> str:
    if isinstance(obj, _FAKE_TYPES):
        return "fake"
    cls = type(obj)
    return f"{cls.__module__}.{cls.__qualname__}"


def mask_url_userinfo(url: str | None) -> str | None:
    """``scheme://user:senha@host/...`` -> ``scheme://***@host/...``; sem credencial, inalterada."""
    if url is None:
        return None
    return _URL_USERINFO.sub(r"\g<scheme>***@", url, count=1)


def _function(fn: Function, *, is_async: bool) -> dict[str, JsonValue]:
    processed = fn.model_copy(deep=True)
    processed.process_entrypoint()  # mesmo esquema que o modelo recebe
    return {"async": is_async, **normalize(processed.to_dict())}  # type: ignore[dict-item]


def _toolkit(tk: Toolkit) -> dict[str, JsonValue]:
    functions = [_function(f, is_async=False) for f in tk.functions.values()]
    functions += [_function(f, is_async=True) for f in tk.async_functions.values()]
    return {
        "__type__": _type_name(tk),
        "name": tk.name,
        "instructions": tk.instructions,
        "add_instructions": tk.add_instructions,
        "functions": sorted(functions, key=lambda f: str(f["name"])),
    }


def normalize(value: Any) -> JsonValue:
    if value is None or isinstance(value, bool | int | float | str):
        return value
    if isinstance(value, Enum):
        return f"{type(value).__name__}.{value.name}"
    if isinstance(value, dict):
        return {str(k): normalize(v) for k, v in value.items()}
    if isinstance(value, list | tuple):
        return [normalize(v) for v in value]
    for kind, convert in _AGNO_NORMALIZERS:
        if isinstance(value, kind):
            return convert(value)
    raise TypeError(f"golden: sem normalização para {_type_name(value)}; trate o tipo em tests/golden/normalize.py")


def _entity(value: Agent | Team) -> JsonValue:
    # membro de Team: identidade basta; os kwargs do membro têm snapshot próprio
    return {"__type__": _type_name(value), "id": value.id, "name": value.name}


def _model(value: Model) -> JsonValue:
    return {"__type__": _type_name(value), "id": value.id, "provider": value.provider}


def _embedder(value: Embedder) -> JsonValue:
    return {
        "__type__": _type_name(value),
        "id": getattr(value, "id", None),
        "provider": getattr(value, "provider", None),
        "dimensions": value.dimensions,
    }


def _db(value: BaseDb | AsyncBaseDb) -> JsonValue:
    return {
        "__type__": _type_name(value),
        "db_url": mask_url_userinfo(getattr(value, "db_url", None)),
        "db_name": getattr(value, "db_name", None),
        "session_table_name": value.session_table_name,
        "memory_table_name": value.memory_table_name,
    }


def _vector_db(value: VectorDb) -> JsonValue:
    return {
        "__type__": _type_name(value),
        "collection_name": getattr(value, "collection_name", None),
        "database": normalize(getattr(value, "database", None)),
        "connection_string": mask_url_userinfo(getattr(value, "connection_string", None)),
        "search_index_name": getattr(value, "search_index_name", None),
        "search_type": normalize(getattr(value, "search_type", None)),
        "distance_metric": normalize(getattr(value, "distance_metric", None)),
        "embedder": normalize(getattr(value, "embedder", None)),
    }


def _knowledge(value: Knowledge) -> JsonValue:
    return {
        "__type__": _type_name(value),
        "name": value.name,
        "max_results": value.max_results,
        "isolate_vector_search": value.isolate_vector_search,
        "contents_db": normalize(value.contents_db),
        "vector_db": normalize(value.vector_db),
    }


def _loose_function(value: Function) -> JsonValue:
    # tool solta (ex.: HTTP, F1-05): instruções entram no system message se add_instructions
    return {
        "__type__": _type_name(value),
        **_function(value, is_async=iscoroutinefunction(value.entrypoint)),
        "instructions": value.instructions,
        "add_instructions": value.add_instructions,
    }


def _guardrail(value: BaseGuardrail) -> JsonValue:
    # só o nome da classe: a montagem muda de módulo na F2 e o snapshot não deve mudar
    attrs = {k: normalize(v) for k, v in sorted(vars(value).items()) if not k.startswith("_")}
    return {"__type__": f"guardrail:{type(value).__qualname__}", **attrs}


# Ordem das checagens de ``isinstance`` (a primeira que casar vale), igual à de antes da divisão.
_AGNO_NORMALIZERS: tuple[tuple[Any, Callable[[Any], JsonValue]], ...] = (
    (Agent | Team, _entity),
    (Model, _model),
    (Embedder, _embedder),
    (BaseDb | AsyncBaseDb, _db),
    (VectorDb, _vector_db),
    (Knowledge, _knowledge),
    (Toolkit, _toolkit),
    (Function, _loose_function),
    (BaseGuardrail, _guardrail),
)
