"""Contrato do ``updated_at`` das portas de config (FA-08): a hora da última gravação no ``Versioned``.

- Mongo: ``list_all`` devolve o ``updated_at`` gravado no documento (datetime; o pymongo sem ``tz_aware``
  o devolve sem fuso, em UTC); ausente ou com valor que não é datetime ⇒ ``None``, sem isolar o documento.
- YAML: sempre ``None`` (o arquivo é do operador; a API não grava nele), mesmo com a chave no documento.
- Backends graváveis (memória e Mongo): o ``save`` devolve o documento gravado com a versão nova e a hora
  da gravação (UTC), a mesma que a ``list_all`` mostra depois; documento semeado sem o campo ⇒ ``None``.
- ``Versioned`` sem ``updated_at`` continua construível, e a igualdade é a do documento (entidade e versão).
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from src.domain.repositories.versioning import Versioned
from tests.fakes.config_stores import (
    agent_document,
    make_agent,
    make_team,
    make_tool,
    team_document,
    tool_document,
)

# Tipo -> (documento de exemplo, método do backend de documentos que monta o repositório).
_DOCUMENTS: dict[str, tuple[Callable[[], dict[str, Any]], str]] = {
    "agent": (lambda: agent_document(make_agent("a")), "agents"),
    "team": (lambda: team_document(make_team("a")), "teams"),
    "tool": (lambda: tool_document(make_tool("a")), "tools"),
}
_WRITTEN = datetime(2026, 11, 26, 14, 3, 0, 250_000)
"""Hora gravada no documento, como o pymongo a devolve (sem fuso, UTC, em milissegundos)."""


def _as_utc(moment: datetime) -> datetime:
    return moment.replace(tzinfo=UTC) if moment.tzinfo is None else moment.astimezone(UTC)


@pytest.fixture(params=sorted(_DOCUMENTS))
def document_kind(request: pytest.FixtureRequest) -> tuple[Callable[[], dict[str, Any]], str]:
    return _DOCUMENTS[request.param]


async def _listed(backend: Any, document_kind: tuple[Callable[[], dict[str, Any]], str], **fields: Any) -> list[Any]:
    make_document, section = document_kind
    repo = getattr(backend, section)([{**make_document(), **fields}])
    return await repo.list_all()


async def test_documento_sem_updated_at_lista_none(backend, document_kind):
    [item] = await _listed(backend, document_kind)

    assert item.updated_at is None and item.version == 0


async def test_updated_at_gravado_vem_do_mongo_e_o_yaml_deixa_none(backend, document_kind):
    [item] = await _listed(backend, document_kind, updated_at=_WRITTEN)

    if backend.name == "yaml":
        assert item.updated_at is None
    else:
        assert _as_utc(item.updated_at) == _WRITTEN.replace(tzinfo=UTC)


@pytest.mark.parametrize("value", ["2026-11-26T14:03:00Z", 1_794_850_061, True, {"$date": 1}, ["x"]])
async def test_updated_at_que_nao_e_datetime_vira_none_sem_isolar_o_documento(backend, document_kind, value):
    [item] = await _listed(backend, document_kind, updated_at=value)

    assert item.updated_at is None and item.entity.id == "a"


async def test_save_devolve_a_gravacao_com_a_hora_que_a_listagem_mostra(writable, kind):
    repo = kind.repo(writable, [kind.make("a"), kind.make("b")])
    before = datetime.now(UTC) - timedelta(milliseconds=1)
    saved = await repo.save(kind.make("a", "v1"), 0)
    after = datetime.now(UTC)

    assert (saved.entity, saved.version) == (kind.make("a", "v1"), 1)
    assert saved.updated_at is not None and saved.updated_at.utcoffset() == timedelta(0)
    assert before <= saved.updated_at <= after
    listed = {item.entity.id: item for item in await repo.list_all()}
    assert listed["b"].updated_at is None  # semeado sem o campo
    # o Mongo guarda em milissegundos: a listagem mostra a mesma hora nessa precisão
    assert abs(_as_utc(listed["a"].updated_at) - saved.updated_at) < timedelta(milliseconds=1)


def test_versioned_sem_updated_at_e_none_e_a_igualdade_e_a_do_documento():
    moment = datetime(2026, 11, 26, 11, 3, tzinfo=timezone(timedelta(hours=-3)))

    assert Versioned("x", 1).updated_at is None
    assert Versioned("x", 1, moment) == Versioned("x", 1)
    assert Versioned("x", 1, moment) != Versioned("x", 2, moment)
    assert hash(Versioned("x", 1, moment)) == hash(Versioned("x", 1))
