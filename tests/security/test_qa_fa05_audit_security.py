"""QA do FA-05, segurança da auditoria: bordas que os testes do dev não cobrem.

Mesma borda real do ``test_admin_config_audit_security.py`` (``admin_world``). Os valores abaixo são canários
fictícios; o que ficou gravado é lido cru de ``world.audit_db``.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.fakes.admin_config_app import ADMIN, ADMIN_KEY, AdminWorld, admin_world, agent_body, if_match, tool_body

BASE = "/admin/config"
CANARY_A = "canario-header-a-0123456789abcdef"
CANARY_B = "canario-header-b-fedcba9876543210"
CANARY_C = "canario-header-c-aaaaaaaabbbbbbbb"
CANARY_ENV = "canario-do-ambiente-zzzzzzzzzzzz"
CANARIES = (CANARY_A, CANARY_B, CANARY_C, CANARY_ENV)


async def _everything_stored(world: AdminWorld) -> str:
    """Tudo que o diário guardou, cru, e tudo que o log do caso de uso registrou."""
    stored = [
        doc
        for name in ("config_audit", "config_revision")
        for doc in await world.audit_db[name].find({}).to_list(length=None)
    ]
    logs = [(r.message, r.context) for r in world.logger.records]
    return repr(stored) + repr(logs)


def _tool(**headers: str) -> dict[str, Any]:
    return tool_body(headers=headers)


async def test_header_adicionado_trocado_removido_e_tool_desativada_nunca_vazam_valor(
    monkeypatch: pytest.MonkeyPatch,
):
    async with admin_world(monkeypatch) as world:
        writes = [
            await world.client.post(f"{BASE}/tools", json=_tool(**{"X-Cliente": CANARY_A}), headers=ADMIN),
            # adiciona um segundo header com segredo e mantém o primeiro mascarado
            await world.client.put(
                f"{BASE}/tools/cep",
                json=_tool(**{"X-Cliente": "***", "X-Sessao": f"Bearer {CANARY_B}"}),
                headers=if_match(1),
            ),
            # troca o valor do segundo
            await world.client.put(
                f"{BASE}/tools/cep",
                json=_tool(**{"X-Cliente": "***", "X-Sessao": f"Bearer {CANARY_C}"}),
                headers=if_match(2),
            ),
            # remove o primeiro
            await world.client.put(f"{BASE}/tools/cep", json=_tool(**{"X-Sessao": "***"}), headers=if_match(3)),
            await world.client.delete(f"{BASE}/tools/cep", headers=if_match(4)),  # remoção lógica
            await world.client.post(f"{BASE}/tools/cep/history/2/restore", headers=if_match(5)),
        ]
        history = await world.client.get(f"{BASE}/tools/cep/history?limit=100", headers=ADMIN)
        listed = await world.client.get(f"{BASE}/tools", headers=ADMIN)
        everything = await _everything_stored(world)

    assert [w.status_code for w in writes] == [201, 200, 200, 200, 200, 422]
    assert [i["version"] for i in history.json()["items"]] == [5, 4, 3, 2, 1]
    for text in (history.text, listed.text, everything, *[w.text for w in writes]):
        assert not any(canary in text for canary in CANARIES)
    removed = next(i for i in history.json()["items"] if i["version"] == 4)
    assert removed["diff"]["headers"] == {
        "before": {"X-Cliente": "***", "X-Sessao": "***"},
        "after": {"X-Sessao": "***"},
    }


async def test_referencia_de_chave_no_agente_fica_como_referencia_e_o_valor_do_ambiente_nunca_entra(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("QA_FA05_MODEL_API_KEY", CANARY_ENV)
    body = agent_body("a1", api_key_ref="env:QA_FA05_MODEL_API_KEY")
    async with admin_world(monkeypatch) as world:
        created = await world.client.post(f"{BASE}/agents", json=body, headers=ADMIN)
        history = await world.client.get(f"{BASE}/agents/a1/history", headers=ADMIN)
        everything = await _everything_stored(world)

    assert created.status_code == 201
    assert history.json()["items"][0]["document"]["api_key_ref"] == "env:QA_FA05_MODEL_API_KEY"
    assert CANARY_ENV not in history.text and CANARY_ENV not in everything


@pytest.mark.parametrize(
    "spoof_headers",
    [
        {"X-Api-Key-Id": "root"},
        {"X-Actor": "root"},
        {"X-Forwarded-User": "root"},
        {"X-API-Key-ID": "root", "Actor": "root"},
    ],
)
async def test_cliente_nao_escolhe_o_ator_por_header_nem_por_corpo(
    monkeypatch: pytest.MonkeyPatch, spoof_headers: dict[str, str]
):
    async with admin_world(monkeypatch) as world:
        by_header = await world.client.post(f"{BASE}/agents", json=agent_body("a1"), headers={**ADMIN, **spoof_headers})
        by_body = await world.client.post(
            f"{BASE}/agents", json=agent_body("a2", actor="root", api_key_id="root", updated_by="root"), headers=ADMIN
        )
        a1 = await world.client.get(f"{BASE}/agents/a1/history", headers=ADMIN)
        a2 = await world.client.get(f"{BASE}/agents/a2/history", headers=ADMIN)

    assert by_header.status_code == 201
    assert a1.json()["items"][0]["actor"] == "admin"
    # campo desconhecido é recusado (modo estrito) e nada é auditado: em nenhum caso vira o ator
    assert (by_body.status_code, by_body.json()["field"]) == (422, "actor")
    assert a2.json()["total"] == 0


@pytest.mark.parametrize("version", ["%EF%BC%91", "%D9%A1", "-1", "1.0", "1e3", "0x1", "+1", "1%00", "1%20", "%24ne"])
async def test_versao_hostil_na_rota_de_restauracao_e_404_sem_gravar_nem_500(
    monkeypatch: pytest.MonkeyPatch, version: str
):
    async with admin_world(monkeypatch) as world:
        await world.client.post(f"{BASE}/agents", json=agent_body("a1"), headers=ADMIN)
        response = await world.client.post(f"{BASE}/agents/a1/history/{version}/restore", headers=if_match(1))
        history = await world.client.get(f"{BASE}/agents/a1/history", headers=ADMIN)

    assert response.status_code == 404
    assert response.json() == {"detail": "Config não encontrada"}
    assert history.json()["total"] == 1


async def test_resposta_de_erro_do_historico_nao_ecoa_id_nem_valor(monkeypatch: pytest.MonkeyPatch):
    marker = "marcador-secreto-no-id-98765"
    async with admin_world(monkeypatch) as world:
        restore = await world.client.post(f"{BASE}/agents/{marker}/history/1/restore", headers=if_match(1))
        missing_header = await world.client.post(f"{BASE}/agents/{marker}/history/1/restore", headers=ADMIN)
        everything = await _everything_stored(world)

    assert restore.status_code == 404 and missing_header.status_code == 428
    assert marker not in restore.text and marker not in everything
    assert ADMIN_KEY not in restore.text + everything
