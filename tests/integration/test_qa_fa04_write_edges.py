"""QA do FA-04: recusas e falhas da escrita que não podem mexer no runtime (borda real, repositórios em memória).

A troca é do ``FakeAgentRuntime`` (``revision`` e ``served`` são o observável): recusa de qualquer tipo, falha
na gravação e config que não monta deixam a revisão e o conjunto servido como estavam, sem o header
``Runtime-Revision``, e nenhuma resposta carrega o texto da exceção da montagem. Chaves são valores de teste.
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from src.domain.ports import InvalidModelConfigError
from tests.fakes.admin_config_app import (
    ADMIN,
    admin_world,
    agent,
    agent_body,
    if_match,
    served_ids,
    team,
    team_body,
    tool,
)
from tests.fakes.runtime import FakeAgentRuntime
from tests.fakes.web import LOOPBACK_BASE_URL, LOOPBACK_CLIENT

BASE = "/admin/config"
TOOL_BODY = {
    "id": "cep",
    "name": "cep",
    "description": "Busca endereço",
    "route": "https://api.example.invalid/cep",
    "http_method": "get",
}
CANARY = "sk-QA-FA04-SEGREDO-DA-MONTAGEM-NAO-PODE-VAZAR"


def _seeded_world(monkeypatch: pytest.MonkeyPatch, runtime: FakeAgentRuntime | None = None):
    return admin_world(
        monkeypatch,
        agents=[agent("a1"), agent("a2")],
        teams=[team("t1", ["a2"])],
        tools=[tool("cep")],
        runtime=runtime,
    )


@pytest.mark.parametrize(
    ("method", "path", "headers", "body", "status"),
    [
        ("PUT", "/agents/a1", if_match(7), agent_body("a1"), 412),
        ("PUT", "/agents/a1", ADMIN, agent_body("a1"), 428),
        ("PUT", "/agents/a1", if_match(0), agent_body("a1", tools_ids=["fantasma"]), 422),
        ("PUT", "/agents/fantasma", if_match(0), agent_body("fantasma"), 404),
        ("POST", "/agents", ADMIN, agent_body("a1"), 409),
        ("POST", "/agents", ADMIN, agent_body("novo", nome=["x"]), 422),
        ("POST", "/teams", ADMIN, team_body("t9", ["fantasma"]), 422),
        ("DELETE", "/tools/fantasma", if_match(0), None, 404),
        ("POST", "/tools", ADMIN, TOOL_BODY, 409),
    ],
)
async def test_recusa_da_escrita_nao_troca_o_runtime_nem_traz_a_revisao(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str, headers: dict[str, str], body: Any, status: int
):
    runtime = FakeAgentRuntime()
    async with _seeded_world(monkeypatch, runtime) as world:
        response = await world.client.request(method, f"{BASE}{path}", json=body, headers=headers)

    assert response.status_code == status
    assert "runtime-revision" not in response.headers
    assert (runtime.revision, runtime.served) == (0, ((), ()))


async def test_delete_de_agente_membro_de_team_ativo_responde_409_e_so_depois_de_tirar_o_team_o_agente_sai(
    monkeypatch: pytest.MonkeyPatch,
):
    runtime = FakeAgentRuntime()
    async with _seeded_world(monkeypatch, runtime) as world:
        blocked_soft = await world.client.delete(f"{BASE}/agents/a2", headers=if_match(0))
        blocked_hard = await world.client.delete(f"{BASE}/agents/a2", params={"hard": "true"}, headers=if_match(0))
        revision_blocked = runtime.revision
        team_removed = await world.client.delete(f"{BASE}/teams/t1", params={"hard": "true"}, headers=if_match(0))
        agent_removed = await world.client.delete(f"{BASE}/agents/a2", params={"hard": "true"}, headers=if_match(0))

    assert blocked_soft.status_code == blocked_hard.status_code == 409
    assert blocked_soft.json()["used_by"] == {"teams": ["t1"]}
    assert revision_blocked == 0
    assert (team_removed.status_code, team_removed.headers["Runtime-Revision"]) == (204, "1")
    assert (agent_removed.status_code, agent_removed.headers["Runtime-Revision"]) == (204, "2")
    assert served_ids(runtime) == (["a1"], [])


async def test_falha_ao_gravar_responde_500_sem_trocar_e_sem_eco_e_a_escrita_seguinte_funciona(
    monkeypatch: pytest.MonkeyPatch,
):
    runtime = FakeAgentRuntime()
    async with _seeded_world(monkeypatch, runtime) as world:
        real_save = world.agents.save
        calls = {"n": 0}

        async def flaky_save(entity: Any, expected_version: int) -> int:
            calls["n"] += 1
            if calls["n"] == 1:
                raise ConnectionError(f"mongo caiu: {CANARY}")
            return await real_save(entity, expected_version)

        monkeypatch.setattr(world.agents, "save", flaky_save)
        transport = httpx.ASGITransport(app=world.app, client=LOOPBACK_CLIENT, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url=LOOPBACK_BASE_URL) as client:
            failed = await client.post(f"{BASE}/agents", json=agent_body("novo"), headers=ADMIN)
            state_after_failure = (runtime.revision, runtime.served)
            ok = await client.post(f"{BASE}/agents", json=agent_body("novo"), headers=ADMIN)

    assert failed.status_code == 500
    assert CANARY not in failed.text and "mongo caiu" not in failed.text
    assert state_after_failure == (0, ((), ()))
    assert (ok.status_code, ok.headers["Runtime-Revision"]) == (201, "1")
    assert "novo" in served_ids(runtime)[0]


@pytest.mark.parametrize("kind", ["agents", "teams"])
@pytest.mark.parametrize(
    "error",
    [
        RuntimeError(f"Authorization: Bearer {CANARY} https://usuario:senha@api.interno/v1"),
        InvalidModelConfigError(f"modelo recusado com chave {CANARY}"),
    ],
    ids=["erro de SDK", "recusa do modelo"],
)
async def test_422_de_config_que_nao_monta_traz_so_o_tipo_e_nada_e_gravado(
    monkeypatch: pytest.MonkeyPatch, kind: str, error: BaseException
):
    runtime = FakeAgentRuntime(failures={"novo": error})
    async with admin_world(monkeypatch, agents=[agent("a1")], runtime=runtime) as world:
        body = agent_body("novo") if kind == "agents" else team_body("novo", ["a1"])
        response = await world.client.post(f"{BASE}/{kind}", json=body, headers=ADMIN)
        stored = await world.client.get(f"{BASE}/{kind}/novo", headers=ADMIN)
        revision = runtime.revision

    assert response.status_code == 422
    assert response.json() == {"detail": "Configuração não monta no runtime", "error_type": type(error).__name__}
    assert CANARY not in response.text and "senha" not in response.text
    assert all(CANARY not in value for value in response.headers.values())
    assert "runtime-revision" not in response.headers
    assert stored.status_code == 404
    assert revision == 0
    # o log da recusa da escrita (kind/refusal/field) também não leva o texto da exceção
    assert all(CANARY not in str(record) for record in world.logger.records)


async def test_escrita_de_tool_recusada_nao_troca_e_a_aceita_troca(monkeypatch: pytest.MonkeyPatch):
    runtime = FakeAgentRuntime()
    agents = [agent("a1", tools_ids=["cep"])]
    async with admin_world(monkeypatch, agents=agents, tools=[tool("cep")], runtime=runtime) as world:
        refused = await world.client.put(f"{BASE}/tools/cep", json={"id": "cep"}, headers=if_match(0))
        removed_in_use = await world.client.delete(f"{BASE}/tools/cep", headers=if_match(0))
        refused_revision = runtime.revision
        updated = await world.client.put(
            f"{BASE}/tools/cep", json={**TOOL_BODY, "description": "Nova descrição"}, headers=if_match(0)
        )

    assert refused.status_code == 422 and removed_in_use.status_code == 409 and refused_revision == 0
    assert (updated.status_code, updated.headers["Runtime-Revision"]) == (200, "1")
    assert served_ids(runtime) == (["a1"], [])
