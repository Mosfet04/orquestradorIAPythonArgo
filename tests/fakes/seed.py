"""Seed do Mongo (``mongo-init/init-db.js``) lido sem Node.

Cada bloco ``db.<coleção>.insertMany([...])`` do seed é JSON puro (sem comentário, ``new Date()`` ou
vírgula sobrando) e é lido com ``json``. Se um bloco deixar de ser JSON, os testes do seed quebram:
mantenha-o assim.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SEED = Path(__file__).resolve().parents[2] / "mongo-init" / "init-db.js"


def insert_many_block(collection: str) -> str:
    """Texto da lista passada a ``db.<collection>.insertMany(...)`` no seed."""
    source = SEED.read_text(encoding="utf-8")
    start = source.index(f"db.{collection}.insertMany(") + len(f"db.{collection}.insertMany(")
    end = source.index("]);", start) + 1
    return source[start:end]


def seed_tool_docs() -> list[dict[str, Any]]:
    """Documentos de ``db.tools.insertMany`` do seed."""
    docs = json.loads(insert_many_block("tools"))
    assert isinstance(docs, list)
    return docs
