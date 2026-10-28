"""FA-05: diário das escritas de config no Mongo (``config_audit`` e o contador de ``config_revision``).

Roda sobre o Mongo falso e, só com ``MONGO_LIVE_URL`` (marker ``live``), sobre um Mongo de verdade.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from src.domain.repositories.config_audit import ConfigAuditEntry, FieldChange
from src.infrastructure.repositories.mongo_config_audit_log import MongoConfigAuditLog
from tests.fakes.config_stores import LIVE_BACKEND, live_mongo_for
from tests.fakes.mongo import FakeMotorClient

AT = datetime(2026, 10, 20, 12, 30, 15, 123000, tzinfo=UTC)


@pytest.fixture(params=["fake", pytest.param(LIVE_BACKEND, marks=pytest.mark.live)])
def database(request: pytest.FixtureRequest) -> Any:
    live = live_mongo_for(request)
    if live is None:
        return FakeMotorClient()["agno"]
    return live.motor()[live.database]


def _entry(entity_id: str = "a1", version: int = 1, *, kind: str = "agents", **extra: Any) -> ConfigAuditEntry:
    fields: dict[str, Any] = {
        "kind": kind,
        "entity_id": entity_id,
        "version": version,
        "actor": "admin",
        "at": AT,
        "diff": {"prompt": FieldChange(None, f"p{version}")},
        "document": {"id": entity_id, "prompt": f"p{version}"},
    }
    fields.update(extra)
    return ConfigAuditEntry(**fields)


async def test_revisao_comeca_em_zero_e_cada_registro_a_incrementa(database: Any) -> None:
    log = MongoConfigAuditLog(database)

    assert await log.revision() == 0
    assert [await log.record(_entry(version=v)) for v in (1, 2, 3)] == [1, 2, 3]
    assert await log.revision() == 3


async def test_historico_do_id_vem_do_mais_novo_ao_mais_antigo_so_do_tipo_e_do_id(database: Any) -> None:
    log = MongoConfigAuditLog(database)
    await log.record(_entry("a1", 1))
    await log.record(_entry("a2", 1))
    await log.record(_entry("a1", 1, kind="teams"))
    await log.record(_entry("a1", 2))
    await log.record(_entry("a1", 2, document=None, diff={"prompt": FieldChange("p2", None)}))

    history = await log.history("agents", "a1")

    assert [(e.version, e.document) for e in history] == [
        (2, None),
        (2, {"id": "a1", "prompt": "p2"}),
        (1, {"id": "a1", "prompt": "p1"}),
    ]
    assert history[0].diff == {"prompt": FieldChange("p2", None)}
    assert (history[2].actor, history[2].kind, history[2].entity_id) == ("admin", "agents", "a1")
    assert history[2].at == AT
    assert await log.history("tools", "a1") == []


async def test_id_que_nao_e_texto_nao_chega_ao_filtro(database: Any) -> None:
    """Como na escrita de config: ``{"$ne": ""}`` casaria o histórico de outro id (injeção NoSQL)."""
    log = MongoConfigAuditLog(database)
    await log.record(_entry("a1"))

    with pytest.raises(TypeError):
        await log.history("agents", {"$ne": ""})  # type: ignore[arg-type]


@pytest.mark.parametrize("value", ["3", 2.0, True, None])
async def test_contador_alterado_por_fora_com_valor_que_nao_e_inteiro_e_recusado(database: Any, value: object) -> None:
    """O poll loga o tipo e tenta de novo; nunca compara uma revisão que não é inteiro."""
    await database["config_revision"].insert_one({"_id": "config", "revision": value})

    with pytest.raises(ValueError, match="revisão da config inválida"):
        await MongoConfigAuditLog(database).revision()
