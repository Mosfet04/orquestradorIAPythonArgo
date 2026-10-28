"""Diário das escritas de config no MongoDB (FA-05): coleção ``config_audit`` e o contador ``config_revision``.

A revisão do store é um documento só (``{"_id": "config", "revision": N}``), incrementado com ``$inc``
atômico antes de cada entrada: N ordena as entradas entre réplicas (o ``_id`` e a data de processos
diferentes não ordenam) e cada réplica só precisa ler esse documento para saber se a config mudou.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime

from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument

from src.domain.repositories.config_audit import ConfigAuditEntry, ConfigAuditLog, FieldChange
from src.infrastructure.repositories.mongo_base import MongoDocument

AUDIT_COLLECTION = "config_audit"
REVISION_COLLECTION = "config_revision"
_REVISION_ID = "config"


def _revision(doc: Mapping[str, object] | None) -> int:
    """Revisão do documento do contador (sem documento, 0)."""
    value = 0 if doc is None else doc.get("revision")
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("revisão da config inválida no MongoDB")
    return value


def _entry(doc: MongoDocument) -> ConfigAuditEntry:
    at: datetime = doc["at"]
    return ConfigAuditEntry(
        kind=doc["kind"],
        entity_id=doc["entity_id"],
        version=doc["version"],
        actor=doc["actor"],
        # O pymongo devolve a data sem fuso (é UTC no BSON).
        at=at if at.tzinfo is not None else at.replace(tzinfo=UTC),
        diff={name: FieldChange(change["before"], change["after"]) for name, change in doc["diff"].items()},
        document=doc["document"],
    )


class MongoConfigAuditLog(ConfigAuditLog):
    """Entradas em ``config_audit`` (com a revisão que cada uma gerou) e a revisão em ``config_revision``."""

    def __init__(self, database: AsyncIOMotorDatabase[MongoDocument]) -> None:
        self._entries = database[AUDIT_COLLECTION]
        self._revisions = database[REVISION_COLLECTION]

    async def record(self, entry: ConfigAuditEntry) -> int:
        """Incrementa a revisão e grava a entrada com ela; devolve a revisão nova."""
        counter = await self._revisions.find_one_and_update(
            {"_id": _REVISION_ID}, {"$inc": {"revision": 1}}, upsert=True, return_document=ReturnDocument.AFTER
        )
        revision = _revision(counter)
        await self._entries.insert_one(
            {
                "revision": revision,
                "kind": entry.kind,
                "entity_id": entry.entity_id,
                "version": entry.version,
                "actor": entry.actor,
                "at": entry.at,
                "diff": {name: {"before": c.before, "after": c.after} for name, c in entry.diff.items()},
                "document": None if entry.document is None else dict(entry.document),
            }
        )
        return revision

    async def history(self, kind: str, entity_id: str) -> list[ConfigAuditEntry]:
        """Entradas do id, da mais nova à mais antiga (pela revisão)."""
        # Só texto chega ao filtro: um operador ({"$ne": ""}) casaria o histórico de outro id.
        if not isinstance(kind, str) or not isinstance(entity_id, str):
            raise TypeError("tipo e id da config devem ser texto")
        cursor = self._entries.find({"kind": kind, "entity_id": entity_id}).sort([("revision", -1)])
        return [_entry(doc) async for doc in cursor]

    async def revision(self) -> int:
        """Revisão atual do store (0 antes da primeira escrita registrada)."""
        return _revision(await self._revisions.find_one({"_id": _REVISION_ID}))
