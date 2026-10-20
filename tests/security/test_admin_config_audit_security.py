"""FA-05, segurança da auditoria: o histórico nunca guarda nem mostra valor de segredo nem a chave de quem escreveu.

Borda real (CORS + auth) com repositórios em memória e o diário do container sobre o Mongo falso
(``tests/fakes/admin_config_app.py``); o que ficou gravado é lido cru do database do diário. A authz das rotas
novas (só a chave admin) está no ``ROUTES`` de ``test_admin_config_security.py``.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.fakes.admin_config_app import ADMIN, ADMIN_KEY, admin_world, if_match, tool_body

BASE = "/admin/config"
FIRST_HEADER_VALUE = "sk-primeiro-valor-de-header-123456"
SECOND_HEADER_VALUE = "sk-segundo-valor-de-header-654321"


async def test_historico_e_diario_nunca_contem_valor_de_header_nem_a_chave_admin(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch) as world:
        writes = [
            await world.client.post(f"{BASE}/tools", json=_with_header(FIRST_HEADER_VALUE), headers=ADMIN),
            await world.client.put(f"{BASE}/tools/cep", json=_with_header(SECOND_HEADER_VALUE), headers=if_match(1)),
            await world.client.post(f"{BASE}/tools/cep/history/1/restore", headers=if_match(2)),
            await world.client.delete(f"{BASE}/tools/cep", params={"hard": "true"}, headers=if_match(3)),
        ]
        history = await world.client.get(f"{BASE}/tools/cep/history", headers=ADMIN)
        stored = [
            doc
            for name in ("config_audit", "config_revision")
            for doc in await world.audit_db[name].find({}).to_list(length=None)
        ]

    assert [w.status_code for w in writes] == [201, 200, 200, 204]
    assert history.status_code == 200 and history.json()["total"] == 4
    created = history.json()["items"][-1]
    assert created["document"]["headers"] == {"X-Api-Key": "***"}
    assert created["diff"]["headers"] == {"before": None, "after": {"X-Api-Key": "***"}}
    changed = history.json()["items"][-2]
    assert changed["diff"] == {"headers": {"before": {"X-Api-Key": "***"}, "after": {"X-Api-Key": "***"}}}
    assert {item["actor"] for item in history.json()["items"]} == {"admin"}
    for text in (history.text, repr(stored), repr([(r.message, r.context) for r in world.logger.records])):
        assert FIRST_HEADER_VALUE not in text and SECOND_HEADER_VALUE not in text
        assert ADMIN_KEY not in text and ADMIN_KEY[:12] not in text


async def test_restaurar_tool_de_outro_host_nao_leva_o_segredo_gravado(monkeypatch: pytest.MonkeyPatch):
    """A versão antiga (outro host) volta com ``***``: o valor gravado agora é do host atual, então 422."""
    async with admin_world(monkeypatch) as world:
        old = {**_with_header(FIRST_HEADER_VALUE), "route": "https://antigo.example.invalid/{cep}"}
        await world.client.post(f"{BASE}/tools", json=old, headers=ADMIN)
        await world.client.put(f"{BASE}/tools/cep", json=_with_header(SECOND_HEADER_VALUE), headers=if_match(1))
        restored = await world.client.post(f"{BASE}/tools/cep/history/1/restore", headers=if_match(2))

    assert restored.status_code == 422
    assert restored.json() == {"detail": "Configuração inválida", "field": "headers"}
    current = (await world.tools.list_all())[0]
    assert (current.entity.route, current.entity.headers, current.version) == (
        tool_body()["route"],
        {"X-Api-Key": SECOND_HEADER_VALUE},
        2,
    )


def _with_header(value: str) -> dict[str, Any]:
    """Corpo da tool com o valor do header ``X-Api-Key``."""
    return tool_body(headers={"X-Api-Key": value})
