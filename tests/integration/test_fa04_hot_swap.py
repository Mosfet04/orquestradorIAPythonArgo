"""FA-04, troca a quente na pilha completa: escrita pela API -> build -> persist -> swap no ``AgnoRuntime`` real.

A pilha é a de ``tests/fakes/hot_swap_app.py`` (``AppFactory`` com lifespan real, repositórios Mongo sobre a
coleção falsa, ``AgnoRuntime``, AgentOS e AG-UI, com o router ``/admin/config``). O modelo é um por montagem:
as instruções que ele recebeu mostram qual instância do agente atendeu o run.
"""

from __future__ import annotations

import asyncio

import pytest

from tests.fakes.admin_config_app import ADMIN, RUN, agent_body, if_match
from tests.fakes.agui import parse_agui_sse, text_of, types_of
from tests.fakes.hot_swap_app import CONFIG, REFUSED_MODEL, agui_body, hot_stack, run_rest


async def test_entidade_criada_depois_do_startup_responde_no_agui_e_no_agentos(monkeypatch: pytest.MonkeyPatch):
    async with hot_stack(monkeypatch) as world:
        before = await world.client.post("/agui/novo", json=agui_body("r0"), headers=RUN)
        created = await world.client.post(
            f"{CONFIG}/agents", json=agent_body("novo", prompt="instrucoes-v1"), headers=ADMIN
        )
        agui = await world.client.post("/agui/novo", json=agui_body("r1"), headers=RUN)
        rest = await run_rest(world.client, "novo")
        health = await world.client.get("/admin/health", headers=ADMIN)

    assert before.status_code == 404
    assert (created.status_code, created.headers["Runtime-Revision"]) == (201, "1")
    events = parse_agui_sse(agui.text)
    assert agui.status_code == 200 and types_of(events)[-1] == "RUN_FINISHED"
    assert text_of(events) == "resposta da montagem 1"
    assert rest.status_code == 200 and rest.json()["content"] == "resposta da montagem 1"
    assert health.json()["runtime_revision"] == 1


async def test_run_em_stream_iniciado_antes_da_troca_termina_com_as_instrucoes_antigas(
    monkeypatch: pytest.MonkeyPatch,
):
    async with hot_stack(monkeypatch) as world:
        await world.client.post(f"{CONFIG}/agents", json=agent_body("a1", prompt="instrucoes-antigas"), headers=ADMIN)
        world.gate.armed = True
        in_flight = asyncio.create_task(world.client.post("/agui/a1", json=agui_body("r1"), headers=RUN))
        await asyncio.wait_for(world.gate.reached.wait(), timeout=5)
        world.gate.armed = False

        updated = await world.client.put(
            f"{CONFIG}/agents/a1", json=agent_body("a1", prompt="instrucoes-novas"), headers=if_match(1)
        )
        world.gate.release.set()
        old_run = await asyncio.wait_for(in_flight, timeout=5)
        next_run = await world.client.post("/agui/a1", json=agui_body("r2"), headers=RUN)

    assert (updated.status_code, updated.headers["Runtime-Revision"]) == (200, "2")
    old_events, next_events = parse_agui_sse(old_run.text), parse_agui_sse(next_run.text)
    assert types_of(old_events)[-1] == "RUN_FINISHED"
    assert text_of(old_events) == "resposta da montagem 1"  # a instância de antes da troca
    assert text_of(next_events) == "resposta da montagem 2"
    seen = world.instructions_seen()
    assert len(seen) == 2
    assert "instrucoes-antigas" in seen[0] and "instrucoes-novas" not in seen[0]
    assert "instrucoes-novas" in seen[1]


async def test_config_que_nao_monta_responde_422_e_o_run_seguinte_usa_a_versao_anterior(
    monkeypatch: pytest.MonkeyPatch,
):
    async with hot_stack(monkeypatch) as world:
        await world.client.post(f"{CONFIG}/agents", json=agent_body("a1", prompt="instrucoes-boas"), headers=ADMIN)
        refused = await world.client.put(
            f"{CONFIG}/agents/a1",
            json=agent_body("a1", prompt="instrucoes-ruins", model=REFUSED_MODEL),
            headers=if_match(1),
        )
        stored = await world.client.get(f"{CONFIG}/agents/a1", headers=ADMIN)
        run = await run_rest(world.client, "a1")
        health = await world.client.get("/admin/health", headers=ADMIN)

    assert refused.status_code == 422
    assert refused.json() == {"detail": "Configuração não monta no runtime", "error_type": "InvalidModelConfigError"}
    assert (stored.json()["prompt"], stored.json()["version"]) == ("instrucoes-boas", 1)
    assert run.status_code == 200 and run.json()["content"] == "resposta da montagem 1"
    assert "instrucoes-boas" in world.instructions_seen()[-1]
    assert health.json()["runtime_revision"] == 1


async def test_entidade_removida_responde_404_no_agentos_no_agui_e_no_cancel(monkeypatch: pytest.MonkeyPatch):
    async with hot_stack(monkeypatch) as world:
        await world.client.post(f"{CONFIG}/agents", json=agent_body("a1"), headers=ADMIN)
        await world.client.post(f"{CONFIG}/agents", json=agent_body("a2"), headers=ADMIN)
        deactivated = await world.client.delete(f"{CONFIG}/agents/a1", headers=if_match(1))
        removed = await world.client.delete(f"{CONFIG}/agents/a2", params={"hard": "true"}, headers=if_match(1))
        responses = {
            entity_id: [
                await world.client.get(f"/agents/{entity_id}", headers=RUN),
                await run_rest(world.client, entity_id),
                await world.client.post(f"/agui/{entity_id}", json=agui_body("r1"), headers=RUN),
                await world.client.post(f"/agents/{entity_id}/runs/r1/cancel", headers=RUN),
            ]
            for entity_id in ("a1", "a2")
        }
        listed = await world.client.get("/agents", headers=RUN)

    assert (deactivated.status_code, removed.status_code) == (200, 204)
    assert removed.headers["Runtime-Revision"] == "4"
    assert {entity_id: [r.status_code for r in found] for entity_id, found in responses.items()} == {
        "a1": [404, 404, 404, 404],
        "a2": [404, 404, 404, 404],
    }
    assert (listed.status_code, listed.json()) == (200, [])


async def test_duas_escritas_simultaneas_sao_serializadas_e_o_runtime_serve_as_duas(monkeypatch: pytest.MonkeyPatch):
    async with hot_stack(monkeypatch) as world:
        first, second = await asyncio.gather(
            world.client.post(f"{CONFIG}/agents", json=agent_body("x"), headers=ADMIN),
            world.client.post(f"{CONFIG}/agents", json=agent_body("y"), headers=ADMIN),
        )
        listed = await world.client.get("/agents", headers=RUN)

    assert (first.status_code, second.status_code) == (201, 201)
    assert sorted(r.headers["Runtime-Revision"] for r in (first, second)) == ["1", "2"]
    assert sorted(a["id"] for a in listed.json()) == ["x", "y"]
