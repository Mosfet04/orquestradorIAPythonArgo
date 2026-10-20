"""QA do FA-04: bordas da troca a quente que os testes do dev não cobrem, na pilha completa.

Mesma pilha de ``test_fa04_hot_swap.py`` (``AppFactory`` com lifespan real, repositórios Mongo sobre a
coleção falsa, ``AgnoRuntime`` e AgentOS reais, modelo falso por montagem), mais o acesso ao store falso para
mudar documentos por fora da API (o que o ``reload`` existe para recarregar) e o transporte que devolve 500 em
vez de relançar a exceção do app. Chaves são valores de teste.
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from tests.fakes.admin_config_app import ADMIN, RUN, agent_body, if_match, team_body
from tests.fakes.agui import parse_agui_sse, text_of, types_of
from tests.fakes.hot_swap_app import CONFIG, agui_body, hot_stack, run_rest
from tests.fakes.mongo import FailingMongoCollection
from tests.fakes.startup_world import agent_doc

DB_URL = "mongodb://usuario:senha-do-banco@mongo.interno:27017"
# O team guarda memória por usuário: o run exige user_id (formulário no AgentOS, forwardedProps no AG-UI).
TEAM_RUN = {"message": "oi", "stream": "false", "user_id": "usuario-qa"}


def _teamagui_body(run_id: str) -> dict[str, Any]:
    return agui_body(run_id, user_id="usuario-qa")


def _member_instructions(team_detail: dict[str, Any]) -> list[str]:
    return [member["system_message"]["instructions"] for member in team_detail["members"]]


# ── cancelamento ─────────────────────────────────────────────────────────────────────────────────


async def test_cancel_de_run_em_andamento_de_entidade_criada_depois_do_startup_chega_ao_gerenciador(
    monkeypatch: pytest.MonkeyPatch,
):
    async with hot_stack(monkeypatch) as stack:
        await stack.client.post(f"{CONFIG}/agents", json=agent_body("novo"), headers=ADMIN)
        stack.gate.armed = True
        in_flight = asyncio.create_task(stack.client.post("/agui/novo", json=agui_body("rc1"), headers=RUN))
        await asyncio.wait_for(stack.gate.reached.wait(), timeout=5)
        stack.gate.armed = False

        cancelled = await stack.client.post("/agents/novo/runs/rc1/cancel", headers=RUN)
        stack.gate.release.set()
        cancelled_run = await asyncio.wait_for(in_flight, timeout=5)
        again = await stack.client.post("/agents/novo/runs/rc1/cancel", headers=RUN)

    assert (cancelled.status_code, cancelled.json()) == (200, {})
    assert cancelled_run.status_code == 200
    cancelled_events = parse_agui_sse(cancelled_run.text)
    assert types_of(cancelled_events) == ["RUN_STARTED", "RUN_ERROR"]
    assert cancelled_events[-1].code == "run_cancelled"
    assert again.status_code == 404
    assert again.json() == {"detail": "run não encontrado ou já encerrado"}


async def test_entidade_removida_com_run_em_andamento_termina_o_run_e_o_cancel_novo_e_404_de_entidade(
    monkeypatch: pytest.MonkeyPatch,
):
    async with hot_stack(monkeypatch) as stack:
        await stack.client.post(f"{CONFIG}/agents", json=agent_body("a1", prompt="velhas"), headers=ADMIN)
        stack.gate.armed = True
        in_flight = asyncio.create_task(stack.client.post("/agui/a1", json=agui_body("rr1"), headers=RUN))
        await asyncio.wait_for(stack.gate.reached.wait(), timeout=5)
        stack.gate.armed = False

        removed = await stack.client.delete(f"{CONFIG}/agents/a1", params={"hard": "true"}, headers=if_match(1))
        cancel_removed = await stack.client.post("/agents/a1/runs/rr1/cancel", headers=RUN)
        new_run = await stack.client.post("/agui/a1", json=agui_body("rr2"), headers=RUN)
        stack.gate.release.set()
        old_run = await asyncio.wait_for(in_flight, timeout=5)

    assert removed.status_code == 204
    assert (cancel_removed.status_code, cancel_removed.json()) == (404, {"detail": "entidade não encontrada"})
    assert new_run.status_code == 404
    events = parse_agui_sse(old_run.text)
    assert old_run.status_code == 200
    assert types_of(events)[-1] == "RUN_FINISHED"
    assert text_of(events) == "resposta da montagem 1"
    assert "velhas" in stack.instructions_seen()[0]


# ── reload ───────────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("path", ["/admin/config/reload", "/admin/refresh-cache"])
async def test_reload_com_store_fora_responde_500_sem_o_erro_do_driver_e_o_runtime_anterior_segue(
    monkeypatch: pytest.MonkeyPatch, path: str
):
    async with hot_stack(monkeypatch, raise_app_exceptions=False) as stack:  # 500 como resposta, não exceção
        await stack.client.post(f"{CONFIG}/agents", json=agent_body("a1", prompt="vigente"), headers=ADMIN)
        working = stack.store["agents_config"]
        # O repositório guarda a coleção: o servidor "cai" nela (find levanta o erro do driver).
        working.find = FailingMongoCollection(ConnectionError(f"sem rota para {DB_URL}")).find  # type: ignore[method-assign]

        failed = await stack.client.post(path, headers=ADMIN)
        revision_after_failure = await stack.revision()
        run = await run_rest(stack.client, "a1")

        del working.find  # volta o find da classe: o servidor voltou
        recovered = await stack.client.post(path, headers=ADMIN)

    assert failed.status_code == 500
    assert "senha-do-banco" not in failed.text and DB_URL not in failed.text
    assert "ConnectionError" not in failed.text
    assert revision_after_failure == 1
    assert run.status_code == 200 and run.json()["content"] == "resposta da montagem 1"
    assert (recovered.status_code, recovered.json()) == (200, {"runtime_revision": 2})


async def test_reload_serve_o_que_foi_mudado_no_store_por_fora_e_tira_o_que_foi_apagado(
    monkeypatch: pytest.MonkeyPatch,
):
    async with hot_stack(monkeypatch) as stack:
        stack.store["agents_config"].docs.append(agent_doc("externo"))
        before = await stack.client.post("/agui/externo", json=agui_body("e0"), headers=RUN)
        reloaded = await stack.client.post("/admin/config/reload", headers=ADMIN)
        served = await stack.client.post("/agui/externo", json=agui_body("e1"), headers=RUN)

        stack.store["agents_config"].docs.clear()
        alias = await stack.client.post("/admin/refresh-cache", headers=ADMIN)
        gone = await stack.client.post("/agui/externo", json=agui_body("e2"), headers=RUN)
        listed = await stack.client.get("/agents", headers=RUN)

    assert before.status_code == 404
    assert (reloaded.status_code, reloaded.json()) == (200, {"runtime_revision": 1})
    assert served.status_code == 200 and types_of(parse_agui_sse(served.text))[-1] == "RUN_FINISHED"
    assert (alias.status_code, alias.json()) == (200, {"runtime_revision": 2})
    assert alias.headers["deprecation"] == "true"
    assert alias.headers["link"] == '</admin/config/reload>; rel="successor-version"'
    assert gone.status_code == 404
    assert listed.json() == []


# ── escrita concorrente com reload ───────────────────────────────────────────────────────────────


async def test_escrita_concorrente_com_reload_nao_perde_a_entidade_nova_e_as_revisoes_nao_se_repetem(
    monkeypatch: pytest.MonkeyPatch,
):
    async with hot_stack(monkeypatch) as stack:
        created, reload_first, created_2, reload_second = await asyncio.gather(
            stack.client.post(f"{CONFIG}/agents", json=agent_body("x"), headers=ADMIN),
            stack.client.post("/admin/config/reload", headers=ADMIN),
            stack.client.post(f"{CONFIG}/agents", json=agent_body("y"), headers=ADMIN),
            stack.client.post("/admin/config/reload", headers=ADMIN),
        )
        listed = await stack.client.get("/agents", headers=RUN)
        final_revision = await stack.revision()

    assert [r.status_code for r in (created, reload_first, created_2, reload_second)] == [201, 200, 201, 200]
    revisions = [
        int(created.headers["Runtime-Revision"]),
        reload_first.json()["runtime_revision"],
        int(created_2.headers["Runtime-Revision"]),
        reload_second.json()["runtime_revision"],
    ]
    assert sorted(revisions) == [1, 2, 3, 4]
    assert sorted(a["id"] for a in listed.json()) == ["x", "y"]
    assert final_revision == 4


# ── teams no AgentOS e no AG-UI depois da troca ──────────────────────────────────────────────────


async def test_team_criado_depois_do_startup_e_servido_e_o_removido_responde_404_em_todas_as_rotas(
    monkeypatch: pytest.MonkeyPatch,
):
    async with hot_stack(monkeypatch) as stack:
        await stack.client.post(f"{CONFIG}/agents", json=agent_body("a1", prompt="velha"), headers=ADMIN)
        created = await stack.client.post(f"{CONFIG}/teams", json=team_body("t1", ["a1"]), headers=ADMIN)
        listed = await stack.client.get("/teams", headers=RUN)
        detail = await stack.client.get("/teams/t1", headers=RUN)
        rest = await stack.client.post("/teams/t1/runs", data=TEAM_RUN, headers=RUN)
        agui = await stack.client.post("/agui/t1", json=_teamagui_body("t-run"), headers=RUN)

        edited = await stack.client.put(
            f"{CONFIG}/agents/a1", json=agent_body("a1", prompt="nova"), headers=if_match(1)
        )
        detail_after_edit = await stack.client.get("/teams/t1", headers=RUN)
        listed_after_edit = await stack.client.get("/teams", headers=RUN)
        rest_after_edit = await stack.client.post(
            "/teams/t1/runs", data=TEAM_RUN, headers=RUN
        )

        removed = await stack.client.delete(f"{CONFIG}/teams/t1", params={"hard": "true"}, headers=if_match(1))
        gone = [
            await stack.client.get("/teams/t1", headers=RUN),
            await stack.client.post("/teams/t1/runs", data=TEAM_RUN, headers=RUN),
            await stack.client.post("/agui/t1", json=_teamagui_body("t-run-2"), headers=RUN),
            await stack.client.post("/teams/t1/runs/t-run/cancel", headers=RUN),
        ]
        teams_after = await stack.client.get("/teams", headers=RUN)
        agents_after = await stack.client.get("/agents", headers=RUN)

    assert (created.status_code, created.headers["Runtime-Revision"]) == (201, "2")
    assert [t["id"] for t in listed.json()] == ["t1"]
    assert detail.status_code == 200 and detail.json()["id"] == "t1"
    assert rest.status_code == 200 and rest.json()["content"].startswith("resposta da montagem")
    assert agui.status_code == 200 and types_of(parse_agui_sse(agui.text))[-1] == "RUN_FINISHED"
    assert (edited.status_code, edited.headers["Runtime-Revision"]) == (200, "3")
    # o modelo do team não delega (não vê as instruções do membro): o membro servido é que mostra a instância nova
    assert _member_instructions(detail.json()) == ["velha"]
    assert _member_instructions(detail_after_edit.json()) == ["nova"]
    assert [t["id"] for t in listed_after_edit.json()] == ["t1"]
    assert rest_after_edit.status_code == 200
    assert (removed.status_code, removed.headers["Runtime-Revision"]) == (204, "4")
    assert [r.status_code for r in gone] == [404, 404, 404, 404]
    assert teams_after.json() == []
    assert [a["id"] for a in agents_after.json()] == ["a1"]
