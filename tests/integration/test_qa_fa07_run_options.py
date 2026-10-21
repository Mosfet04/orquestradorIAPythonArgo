"""QA do FA-07: campos de execução ponta a ponta (API admin -> store -> troca a quente -> agente servido).

O observável é o que o modelo roteirizado recebe: a mensagem de sistema (``markdown``, data/hora) e o
histórico da sessão (``add_history_to_context``, ``num_history_runs``). Documento sem os campos serve
o comportamento de antes (markdown, data/hora e as 5 últimas execuções).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.fakes.admin_config_app import (
    ADMIN,
    RUN,
    admin_world,
    agent,
    agent_body,
    fake_config_admin,
    if_match,
    served_ids,
    team_body,
)
from tests.fakes.config_stores import MongoBackend
from tests.fakes.hot_swap_app import CONFIG, HotStack, hot_stack
from tests.fakes.logger import RecordingLogger

MARKDOWN_LINE = "Use markdown to format your answers."
DATETIME_LINE = "The current time is"
RUN_OPTIONS = ("markdown", "add_history_to_context", "num_history_runs", "add_datetime_to_context", "reasoning")
DEFAULTS: dict[str, Any] = {
    "markdown": True,
    "add_history_to_context": True,
    "num_history_runs": 5,
    "add_datetime_to_context": True,
    "reasoning": False,
}


async def _say(world: HotStack, agent_id: str, message: str, session: str = "s1") -> httpx.Response:
    response = await world.client.post(
        f"/agents/{agent_id}/runs", data={"message": message, "stream": "false", "session_id": session}, headers=RUN
    )
    assert response.status_code == 200
    return response


def _last_call(world: HotStack) -> tuple[str, list[str]]:
    """Sistema e mensagens do usuário da última chamada ao modelo (na ordem em que o agno as montou)."""
    messages = world.models[-1].calls[-1].messages
    system = next(content for role, content in messages if role == "system")
    return system, [content for role, content in messages if role == "user"]


# ── API admin: escrita, leitura, defaults ───────────────────────────────────────────────────────


async def test_campos_gravados_voltam_no_get_na_lista_e_no_put_e_o_runtime_serve_o_agente(
    monkeypatch: pytest.MonkeyPatch,
):
    options = {
        "markdown": False,
        "add_history_to_context": False,
        "num_history_runs": 50,
        "add_datetime_to_context": False,
        "reasoning": True,
    }
    async with admin_world(monkeypatch) as world:
        created = await world.client.post("/admin/config/agents", json=agent_body("a1", **options), headers=ADMIN)
        fetched = await world.client.get("/admin/config/agents/a1", headers=ADMIN)
        listed = await world.client.get("/admin/config/agents", headers=ADMIN)
        low = await world.client.put(
            "/admin/config/agents/a1", json=agent_body("a1", num_history_runs=1), headers=if_match(1)
        )

    assert created.status_code == 201
    for shown in (created.json(), fetched.json(), listed.json()["items"][0]):
        assert {k: shown[k] for k in RUN_OPTIONS} == options
    assert low.status_code == 200
    assert {k: low.json()[k] for k in RUN_OPTIONS} == {**DEFAULTS, "num_history_runs": 1}  # PUT substitui o documento
    assert served_ids(world.runtime) == (["a1"], [])
    assert (await world.agents.get_agent_by_id("a1")).num_history_runs == 1


async def test_corpo_sem_os_campos_grava_e_devolve_os_defaults_de_antes(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch) as world:
        created = await world.client.post("/admin/config/agents", json=agent_body("a1"), headers=ADMIN)
        created_team = await world.client.post(
            "/admin/config/teams", json=team_body("t1", ["a1"]), headers=ADMIN
        )

    assert {k: created.json()[k] for k in RUN_OPTIONS} == DEFAULTS
    assert (created_team.json()["markdown"], created_team.json()["add_history_to_context"]) == (True, True)


async def test_team_grava_e_devolve_markdown_e_historico(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch, agents=[agent("a1")]) as world:
        created = await world.client.post(
            "/admin/config/teams",
            json={**team_body("t1", ["a1"]), "markdown": False, "add_history_to_context": False},
            headers=ADMIN,
        )
        fetched = await world.client.get("/admin/config/teams/t1", headers=ADMIN)

    assert created.status_code == 201
    assert (fetched.json()["markdown"], fetched.json()["add_history_to_context"]) == (False, False)


# ── troca a quente: a pilha real (AgnoRuntime + AgentOS), modelo roteirizado ────────────────────


async def test_agente_sem_os_campos_serve_markdown_data_e_as_cinco_ultimas_execucoes(monkeypatch: pytest.MonkeyPatch):
    async with hot_stack(monkeypatch) as world:
        await world.client.post(f"{CONFIG}/agents", json=agent_body("a1"), headers=ADMIN)
        for i in range(4):
            await _say(world, "a1", f"m{i}")
        # o roteiro do modelo é de 5 respostas por montagem: um PUT sem mudança monta de novo (sessão preservada)
        await world.client.put(f"{CONFIG}/agents/a1", json=agent_body("a1"), headers=if_match(1))
        for i in range(4, 7):
            await _say(world, "a1", f"m{i}")
        system, users = _last_call(world)

    assert MARKDOWN_LINE in system and DATETIME_LINE in system
    assert users == ["m1", "m2", "m3", "m4", "m5", "m6"]  # 5 execuções anteriores + a atual


async def test_put_troca_markdown_data_e_janela_de_historico_no_agente_servido(monkeypatch: pytest.MonkeyPatch):
    async with hot_stack(monkeypatch) as world:
        await world.client.post(
            f"{CONFIG}/agents",
            json=agent_body("a1", markdown=False, add_datetime_to_context=False, num_history_runs=1),
            headers=ADMIN,
        )
        for i in range(3):
            await _say(world, "a1", f"m{i}")
        sparse_system, sparse_users = _last_call(world)

        updated = await world.client.put(
            f"{CONFIG}/agents/a1", json=agent_body("a1", num_history_runs=2), headers=if_match(1)
        )
        await _say(world, "a1", "m3")
        rich_system, rich_users = _last_call(world)

    assert MARKDOWN_LINE not in sparse_system and DATETIME_LINE not in sparse_system
    assert sparse_users == ["m1", "m2"]  # janela de 1 execução + a atual
    assert (updated.status_code, updated.headers["Runtime-Revision"]) == (200, "2")
    assert MARKDOWN_LINE in rich_system and DATETIME_LINE in rich_system
    assert rich_users == ["m1", "m2", "m3"]  # janela de 2 execuções + a atual


async def test_historico_desligado_manda_so_a_mensagem_atual_e_religar_traz_de_volta(monkeypatch: pytest.MonkeyPatch):
    async with hot_stack(monkeypatch) as world:
        await world.client.post(
            f"{CONFIG}/agents", json=agent_body("a1", add_history_to_context=False), headers=ADMIN
        )
        await _say(world, "a1", "m0")
        await _say(world, "a1", "m1")
        _, without = _last_call(world)
        await world.client.put(f"{CONFIG}/agents/a1", json=agent_body("a1"), headers=if_match(1))
        await _say(world, "a1", "m2")
        _, with_history = _last_call(world)

    assert without == ["m1"]
    assert with_history == ["m0", "m1", "m2"]


async def test_markdown_do_team_chega_ao_modelo_do_team_e_a_troca_a_quente_o_muda(monkeypatch: pytest.MonkeyPatch):
    form = {"message": "oi", "stream": "false", "user_id": "u1"}
    async with hot_stack(monkeypatch) as world:
        await world.client.post(f"{CONFIG}/agents", json=agent_body("a1"), headers=ADMIN)
        await world.client.post(
            f"{CONFIG}/teams", json={**team_body("t1", ["a1"]), "markdown": False}, headers=ADMIN
        )
        first = await world.client.post("/teams/t1/runs", data=form, headers=RUN)
        system_off, _ = _last_call(world)
        await world.client.put(f"{CONFIG}/teams/t1", json=team_body("t1", ["a1"]), headers=if_match(1))
        second = await world.client.post("/teams/t1/runs", data=form, headers=RUN)
        system_on, _ = _last_call(world)

    assert (first.status_code, second.status_code) == (200, 200)
    assert MARKDOWN_LINE not in system_off
    assert MARKDOWN_LINE in system_on


# ── documentos legados no Mongo ─────────────────────────────────────────────────────────────────

LEGACY_AGENT = {
    "id": "a1",
    "nome": "Legado",
    "factoryIaModel": "openai",
    "model": "gpt-x",
    "descricao": "d",
    "prompt": "p",
    "active": True,
}
LEGACY_TEAM = {"id": "t1", "nome": "Time", "factoryIaModel": "ollama", "model": "q", "memberIds": ["a1"]}


async def test_documento_mongo_legado_sem_os_campos_e_lido_com_defaults_e_regravado_em_snake_case(
    monkeypatch: pytest.MonkeyPatch,
):
    backend = MongoBackend(monkeypatch)
    logger = RecordingLogger()
    agents = backend.agents([LEGACY_AGENT], logger)
    teams = backend.teams([LEGACY_TEAM], logger)
    tools = backend.tools([], logger)
    async with admin_world(monkeypatch) as world:
        world.app.state.config_admin = fake_config_admin(agents, teams, tools, logger)
        fetched = await world.client.get("/admin/config/agents/a1", headers=ADMIN)
        fetched_team = await world.client.get("/admin/config/teams/t1", headers=ADMIN)
        saved = await world.client.put(
            "/admin/config/agents/a1",
            json={**fetched.json(), "num_history_runs": 9, "markdown": False},
            headers={**ADMIN, "If-Match": fetched.headers["etag"]},
        )

    assert {k: fetched.json()[k] for k in RUN_OPTIONS} == DEFAULTS
    assert (fetched_team.json()["markdown"], fetched_team.json()["add_history_to_context"]) == (True, True)
    assert saved.status_code == 200
    stored = backend.collections["agents_config"].docs[0]
    assert (stored["markdown"], stored["num_history_runs"], stored["reasoning"]) == (False, 9, False)
    assert "factoryIaModel" not in stored and "numHistoryRuns" not in stored
    assert [c.num_history_runs for c in await agents.get_active_agents()] == [9]  # o runtime lê o que a API gravou


async def test_documento_mongo_com_campo_invalido_some_do_admin_e_do_runtime_sem_derrubar_os_vizinhos(
    monkeypatch: pytest.MonkeyPatch,
):
    backend = MongoBackend(monkeypatch)
    logger = RecordingLogger()
    ruim = {**LEGACY_AGENT, "id": "ruim", "num_history_runs": 0}
    agents = backend.agents([LEGACY_AGENT, ruim], logger)
    teams = backend.teams([], logger)
    tools = backend.tools([], logger)
    async with admin_world(monkeypatch) as world:
        world.app.state.config_admin = fake_config_admin(agents, teams, tools, logger)
        listed = await world.client.get("/admin/config/agents", headers=ADMIN)
        missing = await world.client.get("/admin/config/agents/ruim", headers=ADMIN)

    assert [i["id"] for i in listed.json()["items"]] == ["a1"]
    assert missing.status_code == 404
    assert [c.id for c in await agents.get_active_agents()] == ["a1"]
    assert any(r.level == "error" and r.context.get("agent_id") == "ruim" for r in logger.records)
