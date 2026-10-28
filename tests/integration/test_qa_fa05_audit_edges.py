"""QA do FA-05: bordas da auditoria e do histórico que os testes do dev não cobrem, pela borda real.

Mesma pilha de ``test_admin_config_history_routes.py`` (``admin_world``: CORS + auth, repositórios em memória,
diário sobre o Mongo falso). O que ficou gravado é lido cru de ``world.audit_db``. Chaves são valores de teste.
"""

from __future__ import annotations

import asyncio

import httpx
import pytest

from src.infrastructure.web.app_factory import AppFactory
from src.presentation.routes.admin_config import router as admin_config_router
from tests.fakes.admin_config_app import (
    ADMIN,
    RUN,
    AdminWorld,
    admin_env,
    admin_world,
    agent,
    agent_body,
    fake_config_admin,
    if_match,
    team_body,
    tool,
    tool_body,
)
from tests.fakes.logger import RecordingLogger
from tests.fakes.repositories import (
    InMemoryAgentConfigRepository,
    InMemoryTeamConfigRepository,
    InMemoryToolRepository,
)
from tests.fakes.web import LOOPBACK_BASE_URL, LOOPBACK_CLIENT

BASE = "/admin/config"


async def _audit_state(world: AdminWorld) -> tuple[int, int]:
    """(entradas gravadas, revisão do store) como estão no Mongo falso."""
    entries = await world.audit_db["config_audit"].find({}).to_list(length=None)
    counter = await world.audit_db["config_revision"].find_one({"_id": "config"})
    return len(entries), 0 if counter is None else counter["revision"]


# ── escrita recusada não deixa rastro ────────────────────────────────────────────────────────────


async def test_escrita_recusada_nao_gera_entrada_nem_incrementa_a_revisao(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch, agents=[agent("a0")], tools=[tool("t1")]) as world:
        await world.client.post(f"{BASE}/agents", json=agent_body("a1"), headers=ADMIN)
        await world.client.post(f"{BASE}/teams", json=team_body("tm", ["a1"]), headers=ADMIN)
        baseline = await _audit_state(world)
        runtime_revision = world.runtime.revision

        refused = {
            "422 campo inválido": await world.client.post(
                f"{BASE}/agents", json={**agent_body("a2"), "model": 7}, headers=ADMIN
            ),
            "422 referência inexistente": await world.client.post(
                f"{BASE}/agents", json=agent_body("a2", tools_ids=["nao-existe"]), headers=ADMIN
            ),
            "409 id repetido": await world.client.post(f"{BASE}/agents", json=agent_body("a1"), headers=ADMIN),
            "412 versão velha": await world.client.put(
                f"{BASE}/agents/a1", json=agent_body("a1", prompt="x"), headers=if_match(99)
            ),
            "404 sem documento": await world.client.put(
                f"{BASE}/agents/fantasma", json=agent_body("fantasma"), headers=if_match(1)
            ),
            "409 em uso": await world.client.delete(f"{BASE}/agents/a1", params={"hard": "true"}, headers=if_match(1)),
            "restore 404 versão": await world.client.post(f"{BASE}/agents/a1/history/9/restore", headers=if_match(1)),
            "restore 412": await world.client.post(f"{BASE}/agents/a1/history/1/restore", headers=if_match(9)),
            "restore 428": await world.client.post(f"{BASE}/agents/a1/history/1/restore", headers=ADMIN),
        }
        after = await _audit_state(world)
        history = await world.client.get(f"{BASE}/agents/a1/history", headers=ADMIN)

    statuses = {name: response.status_code for name, response in refused.items()}
    assert statuses == {
        "422 campo inválido": 422,
        "422 referência inexistente": 422,
        "409 id repetido": 409,
        "412 versão velha": 412,
        "404 sem documento": 404,
        "409 em uso": 409,
        "restore 404 versão": 404,
        "restore 412": 412,
        "restore 428": 428,
    }
    assert baseline == (2, 2)
    assert after == baseline
    assert world.runtime.revision == runtime_revision
    assert history.json()["total"] == 1


# ── history: borda da rota ───────────────────────────────────────────────────────────────────────


async def test_history_de_tipo_invalido_responde_404_e_paginacao_fora_da_faixa_422(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch) as world:
        await world.client.post(f"{BASE}/agents", json=agent_body("a1"), headers=ADMIN)
        bad_kind = await world.client.get(f"{BASE}/segredos/a1/history", headers=ADMIN)
        restore_bad_kind = await world.client.post(f"{BASE}/segredos/a1/history/1/restore", headers=if_match(1))
        bad_pages = [
            await world.client.get(f"{BASE}/agents/a1/history{query}", headers=ADMIN)
            for query in ("?limit=0", "?limit=100000", "?offset=-1", "?limit=abc", "?offset=1.5")
        ]
        beyond = await world.client.get(f"{BASE}/agents/a1/history?offset=50", headers=ADMIN)

    assert bad_kind.status_code == 404 and restore_bad_kind.status_code == 404
    assert [r.status_code for r in bad_pages] == [422] * 5
    assert beyond.json() == {"items": [], "total": 1, "offset": 50, "limit": 50}


@pytest.mark.parametrize(
    ("entity_id", "status"),
    [
        ("%24ne", 200),
        ("..", 404),  # o cliente normaliza o caminho: a rota nem casa
        ("%2e%2e", 200),
        ("a1%00", 200),
        ("%C3%A9", 200),
        ("x" * 5000, 200),
        ("a1%20", 200),
    ],
)
async def test_history_de_id_hostil_ou_inexistente_e_vazio_sem_vazar_o_de_outro_id(
    monkeypatch: pytest.MonkeyPatch, entity_id: str, status: int
):
    async with admin_world(monkeypatch) as world:
        await world.client.post(f"{BASE}/agents", json=agent_body("a1"), headers=ADMIN)
        response = await world.client.get(f"{BASE}/agents/{entity_id}/history", headers=ADMIN)

    assert response.status_code == status
    if status == 200:
        assert response.json() == {"items": [], "total": 0, "offset": 0, "limit": 50}


async def test_historico_de_um_tipo_nao_aparece_no_do_outro_com_o_mesmo_id(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch) as world:
        await world.client.post(f"{BASE}/agents", json=agent_body("x"), headers=ADMIN)
        await world.client.post(f"{BASE}/teams", json=team_body("x", ["x"]), headers=ADMIN)
        agents = await world.client.get(f"{BASE}/agents/x/history", headers=ADMIN)
        teams = await world.client.get(f"{BASE}/teams/x/history", headers=ADMIN)

    assert agents.json()["total"] == 1 and teams.json()["total"] == 1
    assert "member_ids" not in agents.json()["items"][0]["document"]
    assert teams.json()["items"][0]["document"]["member_ids"] == ["x"]


# ── restore: bordas ──────────────────────────────────────────────────────────────────────────────


async def test_restaurar_entidade_removida_de_vez_responde_404_sem_gravar(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch) as world:
        await world.client.post(f"{BASE}/agents", json=agent_body("a1"), headers=ADMIN)
        await world.client.delete(f"{BASE}/agents/a1", params={"hard": "true"}, headers=if_match(1))
        before = await _audit_state(world)

        history = await world.client.get(f"{BASE}/agents/a1/history", headers=ADMIN)
        restored = await world.client.post(f"{BASE}/agents/a1/history/1/restore", headers=if_match(1))
        restored_removal = await world.client.post(f"{BASE}/agents/a1/history/1/restore", headers=if_match(0))

        after = await _audit_state(world)

    assert [item["document"] is None for item in history.json()["items"]] == [True, False]
    assert (restored.status_code, restored.json()) == (404, {"detail": "Config não encontrada"})
    assert restored_removal.status_code == 404
    assert await world.agents.list_all() == []
    assert after == before


async def test_restaurar_apos_remocao_logica_reativa_o_documento_numa_versao_nova(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch) as world:
        await world.client.post(f"{BASE}/agents", json=agent_body("a1"), headers=ADMIN)
        deactivated = await world.client.delete(f"{BASE}/agents/a1", headers=if_match(1))
        restored = await world.client.post(f"{BASE}/agents/a1/history/1/restore", headers=if_match(2))
        history = await world.client.get(f"{BASE}/agents/a1/history", headers=ADMIN)

    assert deactivated.json()["active"] is False
    assert (restored.status_code, restored.json()["active"], restored.json()["version"]) == (200, True, 3)
    assert [i["version"] for i in history.json()["items"]] == [3, 2, 1]
    assert history.json()["items"][0]["diff"] == {"active": {"before": False, "after": True}}


async def test_restaurar_a_versao_atual_grava_versao_nova_com_diff_vazio(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch) as world:
        await world.client.post(f"{BASE}/agents", json=agent_body("a1"), headers=ADMIN)
        restored = await world.client.post(f"{BASE}/agents/a1/history/1/restore", headers=if_match(1))
        history = await world.client.get(f"{BASE}/agents/a1/history", headers=ADMIN)

    assert (restored.status_code, restored.json()["version"]) == (200, 2)
    assert history.json()["items"][0]["diff"] == {}


async def test_restaurar_tool_mantem_o_valor_de_header_gravado_agora_e_nao_o_da_versao_antiga(
    monkeypatch: pytest.MonkeyPatch,
):
    old, now = "valor-antigo-do-header-aaaa", "valor-atual-do-header-bbbb"
    async with admin_world(monkeypatch) as world:
        await world.client.post(f"{BASE}/tools", json=tool_body(headers={"X-Cliente": old}), headers=ADMIN)
        await world.client.put(f"{BASE}/tools/cep", json=tool_body(headers={"X-Cliente": now}), headers=if_match(1))
        restored = await world.client.post(f"{BASE}/tools/cep/history/1/restore", headers=if_match(2))
        stored = (await world.tools.list_all())[0]

    assert restored.status_code == 200
    assert restored.json()["headers"] == {"X-Cliente": "***"}
    assert stored.entity.headers == {"X-Cliente": now} and stored.version == 3
    assert old not in restored.text


async def test_restaurar_tool_com_header_que_a_tool_atual_nao_tem_e_422_sem_gravar(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch) as world:
        await world.client.post(
            f"{BASE}/tools", json=tool_body(headers={"X-Cliente": "segredo-1234567"}), headers=ADMIN
        )
        await world.client.put(f"{BASE}/tools/cep", json=tool_body(), headers=if_match(1))  # tira o header
        before = await _audit_state(world)
        restored = await world.client.post(f"{BASE}/tools/cep/history/1/restore", headers=if_match(2))
        after = await _audit_state(world)

    assert (restored.status_code, restored.json()["field"]) == (422, "headers")
    assert after == before


# ── ator ─────────────────────────────────────────────────────────────────────────────────────────


async def test_sem_chaves_no_modo_dev_local_o_ator_e_local_dev(monkeypatch: pytest.MonkeyPatch):
    admin_env(monkeypatch)
    for name in ("API_KEY_RUN", "API_KEY_ADMIN"):
        monkeypatch.delenv(name)
    monkeypatch.setenv("ENVIRONMENT", "development")
    agents, teams, tools = InMemoryAgentConfigRepository(), InMemoryTeamConfigRepository(), InMemoryToolRepository()
    app = AppFactory().create_app(routers=[admin_config_router])
    app.state.config_admin = fake_config_admin(agents, teams, tools, RecordingLogger())
    transport = httpx.ASGITransport(app=app, client=LOOPBACK_CLIENT)
    async with httpx.AsyncClient(transport=transport, base_url=LOOPBACK_BASE_URL) as client:
        created = await client.post(f"{BASE}/agents", json=agent_body("a1"))
        history = await client.get(f"{BASE}/agents/a1/history")

    assert created.status_code == 201
    assert [item["actor"] for item in history.json()["items"]] == ["local-dev"]


async def test_chave_run_nao_escreve_nem_le_historico_e_nada_e_auditado(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch) as world:
        written = await world.client.post(f"{BASE}/agents", json=agent_body("a1"), headers=RUN)
        history = await world.client.get(f"{BASE}/agents/a1/history", headers=RUN)
        state = await _audit_state(world)

    assert (written.status_code, history.status_code) == (403, 403)
    assert state == (0, 0)


# ── ordem e concorrência ─────────────────────────────────────────────────────────────────────────


async def test_escritas_concorrentes_no_mesmo_processo_geram_revisoes_sem_buracos_nem_repeticao(
    monkeypatch: pytest.MonkeyPatch,
):
    count = 12
    async with admin_world(monkeypatch) as world:
        responses = await asyncio.gather(
            *(world.client.post(f"{BASE}/agents", json=agent_body(f"c{i}"), headers=ADMIN) for i in range(count))
        )
        raw = await world.audit_db["config_audit"].find({}).to_list(length=None)
        entries, revision = await _audit_state(world)

    assert [r.status_code for r in responses] == [201] * count
    assert (entries, revision) == (count, count)
    assert sorted(doc["revision"] for doc in raw) == list(range(1, count + 1))
    assert sorted(r.headers["Runtime-Revision"] for r in responses) == sorted(str(i) for i in range(1, count + 1))


async def test_dois_updates_concorrentes_com_o_mesmo_if_match_um_vence_e_um_so_registro(
    monkeypatch: pytest.MonkeyPatch,
):
    async with admin_world(monkeypatch) as world:
        await world.client.post(f"{BASE}/agents", json=agent_body("a1"), headers=ADMIN)
        first, second = await asyncio.gather(
            world.client.put(f"{BASE}/agents/a1", json=agent_body("a1", prompt="um"), headers=if_match(1)),
            world.client.put(f"{BASE}/agents/a1", json=agent_body("a1", prompt="dois"), headers=if_match(1)),
        )
        history = await world.client.get(f"{BASE}/agents/a1/history", headers=ADMIN)

    assert sorted([first.status_code, second.status_code]) == [200, 412]
    assert history.json()["total"] == 2
    winner = first if first.status_code == 200 else second
    assert history.json()["items"][0]["document"]["prompt"] == winner.json()["prompt"]
