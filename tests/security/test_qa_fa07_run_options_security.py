"""QA do FA-07, segurança: campo de execução inválido na escrita não ecoa o valor nem grava.

``reasoning`` ligado por texto verdadeiro (``"false"``, ``1``) seria custo extra de chamadas ao modelo sem
o operador pedir; por isso só ``bool`` passa. O valor recusado é um marcador: não pode aparecer na
resposta nem no log da recusa, e o documento anterior fica intacto.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.fakes.admin_config_app import ADMIN, admin_world, agent, agent_body, if_match, team, team_body

BASE = "/admin/config"
CANARY = "valor-hostil-FA07-nao-pode-voltar"

HOSTILE_AGENT_FIELDS: list[tuple[str, Any]] = [
    ("reasoning", "false"),
    ("reasoning", 1),
    ("reasoning", CANARY),
    ("markdown", CANARY),
    ("markdown", {"x": CANARY}),
    ("add_history_to_context", [CANARY]),
    ("add_datetime_to_context", None),
    ("num_history_runs", CANARY),
    ("num_history_runs", {"x": CANARY}),
    ("num_history_runs", True),
    ("num_history_runs", 0),
    ("num_history_runs", 51),
    ("num_history_runs", -(10**30)),
    ("num_history_runs", 10**30),
    ("num_history_runs", 5.5),
    ("num_history_runs", None),
]


@pytest.mark.parametrize(("field", "value"), HOSTILE_AGENT_FIELDS, ids=lambda v: repr(v)[:30])
async def test_agente_com_campo_de_execucao_hostil_responde_422_no_campo_sem_eco_e_sem_gravar(
    monkeypatch: pytest.MonkeyPatch, field: str, value: Any
):
    async with admin_world(monkeypatch, agents=[agent("a1")]) as world:
        created = await world.client.post(f"{BASE}/agents", json=agent_body("novo", **{field: value}), headers=ADMIN)
        updated = await world.client.put(
            f"{BASE}/agents/a1", json=agent_body("a1", **{field: value}), headers=if_match(0)
        )

    for response in (created, updated):
        assert response.status_code == 422
        assert response.json() == {"detail": "Configuração inválida", "field": field}
        assert CANARY not in response.text
    assert CANARY not in repr(world.logger.records)
    assert [(i.entity.id, i.version) for i in await world.agents.list_all()] == [("a1", 0)]
    assert world.runtime.revision == 0


@pytest.mark.parametrize(
    ("field", "value"),
    [("markdown", CANARY), ("markdown", 0), ("add_history_to_context", "true"), ("add_history_to_context", None)],
    ids=repr,
)
async def test_team_com_campo_de_execucao_hostil_responde_422_no_campo_sem_eco_e_sem_gravar(
    monkeypatch: pytest.MonkeyPatch, field: str, value: Any
):
    async with admin_world(monkeypatch, agents=[agent("a1")], teams=[team("t1", ["a1"])]) as world:
        created = await world.client.post(
            f"{BASE}/teams", json={**team_body("novo", ["a1"]), field: value}, headers=ADMIN
        )
        updated = await world.client.put(
            f"{BASE}/teams/t1", json={**team_body("t1", ["a1"]), field: value}, headers=if_match(0)
        )

    for response in (created, updated):
        assert response.status_code == 422
        assert response.json() == {"detail": "Configuração inválida", "field": field}
        assert CANARY not in response.text
    assert CANARY not in repr(world.logger.records)
    assert [(i.entity.id, i.version) for i in await world.teams.list_all()] == [("t1", 0)]
    assert world.runtime.revision == 0


@pytest.mark.parametrize("field", ["num_history_runs", "reasoning", "add_datetime_to_context"])
async def test_campo_de_agente_no_corpo_de_team_e_recusado_como_chave_desconhecida(
    monkeypatch: pytest.MonkeyPatch, field: str
):
    """Team não fixa esses campos (passá-los mudaria o golden): a chave é recusada, não descartada em silêncio."""
    value: Any = 3 if field == "num_history_runs" else True
    async with admin_world(monkeypatch, agents=[agent("a1")]) as world:
        response = await world.client.post(
            f"{BASE}/teams", json={**team_body("novo", ["a1"]), field: value}, headers=ADMIN
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "Configuração inválida", "field": field}
    assert await world.teams.list_all() == []


@pytest.mark.parametrize("key", ["numHistoryRuns", "addHistoryToContext", "addDatetimeToContext"])
async def test_grafia_camel_case_dos_campos_novos_e_recusada_na_escrita(monkeypatch: pytest.MonkeyPatch, key: str):
    async with admin_world(monkeypatch) as world:
        response = await world.client.post(f"{BASE}/agents", json=agent_body("a1", **{key: 3}), headers=ADMIN)

    assert response.status_code == 422
    assert response.json() == {"detail": "Configuração inválida", "field": key}
    assert await world.agents.list_all() == []


@pytest.mark.parametrize(("field", "value"), [("markdown", {"$ne": CANARY}), ("num_history_runs", {"$gt": 0})])
async def test_operador_mongo_no_valor_do_campo_de_execucao_responde_422_sem_gravar(
    monkeypatch: pytest.MonkeyPatch, field: str, value: Any
):
    async with admin_world(monkeypatch) as world:
        response = await world.client.post(f"{BASE}/agents", json=agent_body("a1", **{field: value}), headers=ADMIN)

    assert response.status_code == 422
    assert CANARY not in response.text
    assert await world.agents.list_all() == []
