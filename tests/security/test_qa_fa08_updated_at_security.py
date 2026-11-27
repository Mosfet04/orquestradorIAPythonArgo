"""QA FA-08, segurança do ``updated_at`` do corpo de escrita: valor hostil nunca chega ao documento nem volta.

Mongo fake com os repositórios reais: o ``updated_at`` gravado é sempre o do servidor (datetime), e o texto
enviado (operador Mongo, forjar hora, payload grande) não é gravado, logado nem ecoado. Sem chave admin,
a resposta não mostra o documento.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

import pytest

from tests.fakes.admin_config_app import ADMIN, RUN, admin_world, agent_body, fake_config_admin, if_match
from tests.fakes.config_stores import MongoBackend
from tests.fakes.logger import RecordingLogger

BASE = "/admin/config"
CANARY = "canario-updated-at-9f3a"
HOSTILE: list[Any] = [
    {"$gt": ""},
    {"$set": {"version": 99}},
    [CANARY],
    CANARY,
    CANARY * 5000,
    "2099-01-01T00:00:00Z",
    None,
    1e308,
]


@pytest.mark.parametrize("hostile", HOSTILE, ids=range(len(HOSTILE)))
async def test_updated_at_hostil_no_corpo_nao_e_gravado_nem_ecoado(monkeypatch: pytest.MonkeyPatch, hostile: Any):
    backend = MongoBackend(monkeypatch)
    logger = RecordingLogger()
    agents, teams, tools = backend.agents([], logger), backend.teams([], logger), backend.tools([], logger)
    async with admin_world(monkeypatch) as world:
        world.app.state.config_admin = fake_config_admin(agents, teams, tools, logger)
        created = await world.client.post(f"{BASE}/agents", json=agent_body("a1", updated_at=hostile), headers=ADMIN)
        replaced = await world.client.put(
            f"{BASE}/agents/a1", json=agent_body("a1", updated_at=hostile), headers=if_match(1)
        )

    assert (created.status_code, replaced.status_code) == (201, 200)
    [stored] = backend.collections["agents_config"].docs
    assert isinstance(stored["updated_at"], datetime) and stored["version"] == 2
    assert stored["updated_at"].year < 2099
    for response in (created, replaced):
        assert CANARY not in response.text and "2099" not in response.text
    assert CANARY not in repr(logger.records)


async def test_sem_chave_admin_a_resposta_nao_mostra_documento_nem_updated_at(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch) as world:
        anonymous = await world.client.get(f"{BASE}/agents")
        run_key = await world.client.get(f"{BASE}/agents", headers=RUN)

    for response in (anonymous, run_key):
        assert response.status_code in (401, 403) and "updated_at" not in response.text
