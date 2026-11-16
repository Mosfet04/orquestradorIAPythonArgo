"""Sessões como o agno 2.5.8 as gravava (runs no campo ``runs`` do documento), para a migração (F3-06).

``agno_2_5_8_sessions.json`` foi gerado com a venv do agno 2.5.8 (``AsyncMongoDb`` do agno sobre o
motor, Atlas Local 8.0.32): um ``Agent`` ``ag`` com modelo roteirizado rodou três vezes com ``run_id``
do cliente (``r-ana-1`` e ``r-ana-2`` na sessão ``s-ana`` da ``ana``; ``r-bruno-1`` na ``s-bruno`` do
``bruno``). São os documentos de ``agno_sessions`` como saíram do banco (``find`` sem o ``_id``, JSON
relaxado do ``bson.json_util``); o 2.5.8 não cria ``agno_runs`` nem carimba versão de esquema.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

LEGACY_SESSIONS_FILE = Path(__file__).with_name("agno_2_5_8_sessions.json")
LEGACY_RUNS = {
    "r-ana-1": ("s-ana", "ana", "O-CODIGO-DA-ANA-E-7421"),
    "r-ana-2": ("s-ana", "ana", "anotado"),
    "r-bruno-1": ("s-bruno", "bruno", "oi bruno"),
}
"""``run_id`` → (sessão, ``user_id``, resposta) de cada run da fixture."""


def legacy_sessions() -> list[dict[str, Any]]:
    """Documentos novos (cópia a cada chamada) de ``agno_sessions`` gravados pelo 2.5.8."""
    docs: list[dict[str, Any]] = json.loads(LEGACY_SESSIONS_FILE.read_text(encoding="utf-8"))
    return docs


async def seed_legacy_sessions(sessions: Any, docs: list[dict[str, Any]] | None = None) -> None:
    """Grava os documentos (os da fixture, por padrão) na coleção de sessões dada (fake ou real)."""
    await sessions.insert_many(docs if docs is not None else legacy_sessions())
