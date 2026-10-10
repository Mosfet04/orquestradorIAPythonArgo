"""Seed do Mongo (F1-10): ``teams_config`` no formato canônico, lido pelo mapper real.

Canônico = o dos agentes e do README: ``factoryIaModel`` e o resto em snake_case
(``member_ids``, ``user_memory_active``, ``summary_active``). O mapper ainda aceita o legado
camelCase (``memberIds``, ``userMemoryActive``, ``summaryActive``) para documentos antigos;
o seed não o usa mais. O bloco ``db.teams_config.insertMany([...])`` é JSON puro.
"""

from __future__ import annotations

import json
import re
from typing import Any

from src.infrastructure.repositories.config_documents import map_team_document
from tests.fakes.logger import RecordingLogger
from tests.unit.test_seed_tools import _insert_many_block

NON_CANONICAL_KEYS = {"memberIds", "userMemoryActive", "summaryActive", "factory_ia_model"}


def _seed_team_docs() -> list[dict[str, Any]]:
    docs = json.loads(_insert_many_block("teams_config"))
    assert isinstance(docs, list) and docs
    return docs


def _seed_agent_ids() -> list[str]:
    return re.findall(r'^\s{4}"id":\s*"([^"]+)"', _insert_many_block("agents_config"), re.MULTILINE)


def test_seed_de_teams_usa_so_o_formato_canonico():
    for doc in _seed_team_docs():
        assert not NON_CANONICAL_KEYS & doc.keys(), doc["id"]
        assert {"member_ids", "user_memory_active", "summary_active", "factoryIaModel"} <= doc.keys(), doc["id"]


def test_seed_de_teams_passa_pelo_mapper_sem_perder_campos():
    for doc in _seed_team_docs():
        team = map_team_document(doc, RecordingLogger())

        assert team.id == doc["id"]
        assert team.member_ids == doc["member_ids"]
        assert team.user_memory_active is doc["user_memory_active"]
        assert team.summary_active is doc["summary_active"]
        assert team.factory_ia_model == doc["factoryIaModel"]
        assert team.mode == doc["mode"]
        assert team.active is True


def test_membros_dos_teams_do_seed_existem_no_seed_de_agentes():
    agent_ids = set(_seed_agent_ids())
    assert agent_ids == {"general-assistant", "code-assistant", "analyst-assistant"}
    for doc in _seed_team_docs():
        assert set(doc["member_ids"]) <= agent_ids, doc["id"]
