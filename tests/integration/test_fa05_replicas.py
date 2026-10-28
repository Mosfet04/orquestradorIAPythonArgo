"""FA-05, propagação entre réplicas na pilha completa: dois apps (lifespan real cada) no mesmo Mongo falso.

A pilha é a de ``tests/fakes/hot_swap_app.py`` (``replica_stacks``): repositórios e diário Mongo sobre as coleções
falsas, ``AgnoRuntime``, AgentOS e AG-UI por réplica. Com o poll parado (default), o teste dá a volta dele com
``app.state.config_admin.sync()``; com ``poll_seconds`` curto, é o laço do lifespan que troca.
"""

from __future__ import annotations

import pytest

from tests.fakes.admin_config_app import ADMIN, RUN, agent_body, if_match
from tests.fakes.agui import parse_agui_sse, types_of
from tests.fakes.hot_swap_app import CONFIG, POLL_SECONDS, agui_body, replica_stacks, run_rest, served_within


async def test_segunda_replica_serve_a_mudanca_numa_volta_do_poll(monkeypatch: pytest.MonkeyPatch):
    async with replica_stacks(monkeypatch, count=2) as (writer, reader):
        created = await writer.client.post(f"{CONFIG}/agents", json=agent_body("a1", prompt="v1"), headers=ADMIN)
        before = await reader.client.post("/agui/a1", json=agui_body("r0"), headers=RUN)

        assert await reader.app.state.config_admin.sync() == 1
        agui = await reader.client.post("/agui/a1", json=agui_body("r1"), headers=RUN)
        rest = await run_rest(reader.client, "a1")
        health = await reader.client.get("/admin/health", headers=ADMIN)
        again = await reader.app.state.config_admin.sync()
        own = await writer.app.state.config_admin.sync()

        v2 = agent_body("a1", prompt="v2")
        updated = await reader.client.put(f"{CONFIG}/agents/a1", json=v2, headers=if_match(1))
        assert await writer.app.state.config_admin.sync() == 2
        run_after_update = await run_rest(writer.client, "a1")

    assert created.status_code == 201 and before.status_code == 404
    assert agui.status_code == 200 and types_of(parse_agui_sse(agui.text))[-1] == "RUN_FINISHED"
    assert rest.status_code == 200
    assert health.json()["runtime_revision"] == 1
    assert (again, own) == (None, None)  # nada novo; a própria escrita já era servida
    assert updated.status_code == 200
    assert run_after_update.status_code == 200
    assert "v2" in writer.instructions_seen()[-1]


async def test_remocao_numa_replica_tira_a_entidade_da_outra(monkeypatch: pytest.MonkeyPatch):
    async with replica_stacks(monkeypatch, count=2) as (writer, reader):
        await writer.client.post(f"{CONFIG}/agents", json=agent_body("a1"), headers=ADMIN)
        await reader.app.state.config_admin.sync()
        served = await reader.client.get("/agents/a1", headers=RUN)

        await writer.client.delete(f"{CONFIG}/agents/a1", params={"hard": "true"}, headers=if_match(1))
        await reader.app.state.config_admin.sync()
        gone = await reader.client.get("/agents/a1", headers=RUN)

    assert (served.status_code, gone.status_code) == (200, 404)


async def test_poll_do_lifespan_serve_a_escrita_da_outra_replica_em_um_ciclo(monkeypatch: pytest.MonkeyPatch):
    async with replica_stacks(monkeypatch, count=2, poll_seconds=str(POLL_SECONDS)) as (writer, reader):
        created = await writer.client.post(f"{CONFIG}/agents", json=agent_body("a1"), headers=ADMIN)
        served = await served_within(reader.client, "/agents/a1")
        health = await reader.client.get("/admin/health", headers=ADMIN)

    assert created.status_code == 201
    assert served.status_code == 200
    assert health.json()["runtime_revision"] == 1
