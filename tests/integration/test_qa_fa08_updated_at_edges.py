"""QA FA-08: bordas do ``updated_at`` na API Admin que os testes do item não fixam.

- Descarte do ``updated_at`` do corpo também em teams e tools, só na raiz (chave aninhada de dado do
  usuário, como header de tool, é preservada) e sem afrouxar o modo estrito para o resto (chave
  desconhecida, ``Updated_At`` e ``updatedAt`` seguem 422).
- ``DELETE ?hard=true`` segue 204 sem corpo; a página da lista e o documento legado
  regravado mostram a hora certa; respostas de erro não ganham o campo.
- Mongo fake: o ``save`` devolve exatamente a hora que ficou no documento (milissegundos); escrita recusada
  por versão velha não toca a hora; datetime com fuso sai convertido em UTC.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta, timezone
from typing import Any

import pytest

from src.presentation.routes.admin_config import updated_at_text
from tests.fakes.admin_config_app import (
    ADMIN,
    admin_world,
    agent,
    agent_body,
    fake_config_admin,
    if_match,
    team_body,
    tool_body,
)
from tests.fakes.config_stores import MongoBackend, agent_document
from tests.fakes.logger import RecordingLogger

BASE = "/admin/config"
SENT = "1999-01-01T00:00:00Z"


def _mongo(monkeypatch: pytest.MonkeyPatch, agents: list[dict[str, Any]] | None = None):
    backend = MongoBackend(monkeypatch)
    logger = RecordingLogger()
    repos = (backend.agents(agents or [], logger), backend.teams([], logger), backend.tools([], logger))
    return backend, repos, logger


# ── descarte do corpo ────────────────────────────────────────────────────────────────────────


async def test_updated_at_do_corpo_e_descartado_em_teams_e_tools_no_post_e_no_put(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch, agents=[agent("a1")]) as world:
        team = await world.client.post(
            f"{BASE}/teams", json={**team_body("t1", ["a1"]), "updated_at": SENT}, headers=ADMIN
        )
        tool = await world.client.post(f"{BASE}/tools", json={**tool_body(), "updated_at": SENT}, headers=ADMIN)
        team_put = await world.client.put(
            f"{BASE}/teams/t1", json={**team_body("t1", ["a1"]), "updated_at": [1]}, headers=if_match(1)
        )
        tool_put = await world.client.put(
            f"{BASE}/tools/cep", json={**tool_body(), "updated_at": None}, headers=if_match(1)
        )

    assert [r.status_code for r in (team, tool, team_put, tool_put)] == [201, 201, 200, 200]
    for response in (team, tool, team_put, tool_put):
        assert response.json()["updated_at"] not in (SENT, None, [1])
        assert updated_at_text(datetime.now(UTC)) >= response.json()["updated_at"]


async def test_so_a_raiz_e_descartada_updated_at_aninhado_de_dado_do_usuario_e_preservado(
    monkeypatch: pytest.MonkeyPatch,
):
    async with admin_world(monkeypatch) as world:
        created = await world.client.post(
            f"{BASE}/tools",
            json=tool_body(headers={"updated_at": "x-valor", "version": "9"}, updated_at=SENT),
            headers=ADMIN,
        )

    assert created.status_code == 201
    stored = (await world.tools.list_all())[0].entity
    assert set(stored.headers or {}) == {"updated_at", "version"}
    assert created.json()["updated_at"] != SENT


@pytest.mark.parametrize("key", ["Updated_At", "updatedAt", "updated_at ", "UPDATED_AT", "updated"])
async def test_variantes_do_nome_nao_sao_descartadas_e_o_modo_estrito_as_recusa(
    monkeypatch: pytest.MonkeyPatch, key: str
):
    async with admin_world(monkeypatch) as world:
        response = await world.client.post(f"{BASE}/agents", json=agent_body("a1", **{key: SENT}), headers=ADMIN)
        listed = await world.client.get(f"{BASE}/agents", headers=ADMIN)

    assert response.status_code == 422
    assert SENT not in response.text  # nomeia o campo recusado, nunca o valor
    assert listed.json()["items"] == []


async def test_chave_desconhecida_continua_422_mesmo_junto_do_updated_at(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch) as world:
        response = await world.client.post(
            f"{BASE}/agents", json=agent_body("a1", updated_at=SENT, desconhecida=1), headers=ADMIN
        )

    assert response.status_code == 422


async def test_corpo_que_nao_e_objeto_continua_recusado_com_updated_at_dentro(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch) as world:
        response = await world.client.post(f"{BASE}/agents", json=[{"updated_at": SENT}], headers=ADMIN)

    assert response.status_code == 422


# ── respostas ────────────────────────────────────────────────────────────────────────────────


async def test_delete_hard_responde_sem_corpo_e_o_documento_some_com_a_hora(monkeypatch: pytest.MonkeyPatch):
    backend, (agents, teams, tools), logger = _mongo(monkeypatch)
    async with admin_world(monkeypatch) as world:
        world.app.state.config_admin = fake_config_admin(agents, teams, tools, logger)
        await world.client.post(f"{BASE}/agents", json=agent_body("a1"), headers=ADMIN)
        removed = await world.client.delete(f"{BASE}/agents/a1", params={"hard": "true"}, headers=if_match(1))
        gone = await world.client.get(f"{BASE}/agents/a1", headers=ADMIN)

    assert removed.status_code == 204 and removed.content == b""
    assert gone.status_code == 404 and "updated_at" not in gone.text
    assert backend.collections["agents_config"].docs == []


async def test_respostas_de_erro_nao_ganham_updated_at(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch, agents=[agent("a1")]) as world:
        stale = await world.client.put(f"{BASE}/agents/a1", json=agent_body("a1"), headers=if_match(7))
        missing = await world.client.get(f"{BASE}/agents/nada", headers=ADMIN)

    assert (stale.status_code, missing.status_code) == (412, 404)
    assert "updated_at" not in stale.text and "updated_at" not in missing.text


async def test_pagina_da_lista_mostra_a_hora_de_cada_item_gravado(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch, agents=[agent("legado")]) as world:
        for entity_id in ("n1", "n2"):
            await world.client.post(f"{BASE}/agents", json=agent_body(entity_id), headers=ADMIN)
        page = await world.client.get(f"{BASE}/agents", params={"offset": 1, "limit": 2}, headers=ADMIN)

    items = page.json()["items"]
    assert [i["id"] for i in items] == ["n1", "n2"]
    assert all(i["updated_at"].endswith("Z") for i in items) and page.json()["total"] == 3


async def test_documento_legado_regravado_passa_de_null_para_a_hora_da_gravacao(monkeypatch: pytest.MonkeyPatch):
    _, (agents, teams, tools), logger = _mongo(monkeypatch, [agent_document(agent("a1"))])
    async with admin_world(monkeypatch) as world:
        world.app.state.config_admin = fake_config_admin(agents, teams, tools, logger)
        before = await world.client.get(f"{BASE}/agents/a1", headers=ADMIN)
        replaced = await world.client.put(f"{BASE}/agents/a1", json=agent_body("a1"), headers=if_match(0))
        after = await world.client.get(f"{BASE}/agents/a1", headers=ADMIN)

    assert before.json()["updated_at"] is None and before.json()["version"] == 0
    assert replaced.status_code == 200 and replaced.json()["version"] == 1
    assert after.json()["updated_at"] == replaced.json()["updated_at"] and after.json()["updated_at"] is not None
    assert logger.messages("warning", "error") == []


# ── Mongo fake ───────────────────────────────────────────────────────────────────────────────


async def test_mongo_save_devolve_exatamente_a_hora_que_ficou_no_documento(monkeypatch: pytest.MonkeyPatch):
    backend, (agents, _teams, _tools), _logger = _mongo(monkeypatch)

    created = await agents.save(agent("a1"), 0)
    rewritten = await agents.save(agent("a1"), 1)

    stored = backend.collections["agents_config"].docs[0]["updated_at"]
    assert rewritten.updated_at == stored
    assert created.updated_at is not None and created.updated_at <= rewritten.updated_at
    assert stored.microsecond % 1000 == 0  # milissegundos, a precisão do BSON
    assert [i.updated_at for i in await agents.list_all()] == [stored]


async def test_mongo_escrita_recusada_por_versao_velha_nao_toca_a_hora(monkeypatch: pytest.MonkeyPatch):
    _backend, (agents, teams, tools), logger = _mongo(monkeypatch)
    async with admin_world(monkeypatch) as world:
        world.app.state.config_admin = fake_config_admin(agents, teams, tools, logger)
        created = await world.client.post(f"{BASE}/agents", json=agent_body("a1"), headers=ADMIN)
        stale = await world.client.put(f"{BASE}/agents/a1", json=agent_body("a1", prompt="x"), headers=if_match(5))
        fetched = await world.client.get(f"{BASE}/agents/a1", headers=ADMIN)

    assert stale.status_code == 412
    assert fetched.json()["updated_at"] == created.json()["updated_at"] and fetched.json()["version"] == 1


async def test_mongo_datetime_com_fuso_sai_convertido_para_utc(monkeypatch: pytest.MonkeyPatch):
    sao_paulo = datetime(2026, 11, 26, 23, 30, 5, tzinfo=timezone(timedelta(hours=-3)))
    _, (agents, teams, tools), logger = _mongo(monkeypatch, [{**agent_document(agent("a1")), "updated_at": sao_paulo}])
    async with admin_world(monkeypatch) as world:
        world.app.state.config_admin = fake_config_admin(agents, teams, tools, logger)
        fetched = await world.client.get(f"{BASE}/agents/a1", headers=ADMIN)

    assert fetched.json()["updated_at"] == "2026-11-27T02:30:05Z"
