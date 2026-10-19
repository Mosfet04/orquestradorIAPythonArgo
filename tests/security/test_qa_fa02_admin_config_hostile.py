"""QA do FA-02, segurança: entradas hostis nas rotas ``/admin/config``.

Reproduz os defeitos corrigidos na rodada 3: BUG-R9 (chave desconhecida do cliente vai
sem saneamento para o ``field`` do 422 e para o log) e BUG-FA02-NONFINITE (``NaN``/``Infinity`` e
surrogate solitário passam pela validação, são gravados e depois estouram a serialização da resposta).
"""

from __future__ import annotations

from typing import Any

import httpx
import pytest

from tests.fakes.admin_config_app import ADMIN, admin_world, agent, agent_body
from tests.fakes.web import LOOPBACK_BASE_URL, LOOPBACK_CLIENT

BASE = "/admin/config"
JSON = {"Content-Type": "application/json"}
FORGED_KEY = "promt\nlevel=critical event=forjado"
LONG_KEY = "k" * 200_000


@pytest.mark.parametrize(
    "key",
    [FORGED_KEY, LONG_KEY, "chave com espaço", "\x1b[31mcor", "a" * 65, "1abc"],
    ids=["quebra-de-linha", "200000-caracteres", "espaco", "escape-ansi", "65-caracteres", "comeca-com-digito"],
)
async def test_chave_desconhecida_do_cliente_nao_volta_crua_na_resposta_nem_no_log(
    monkeypatch: pytest.MonkeyPatch, key: str
):
    async with admin_world(monkeypatch) as world:
        response = await world.client.post(f"{BASE}/agents", json=agent_body("a1", **{key: 1}), headers=ADMIN)

    assert response.status_code == 422
    assert response.json() == {"detail": "Configuração inválida", "field": "unknown_key"}
    assert key not in response.text
    refused = [r for r in world.logger.records if r.message == "Escrita de config recusada"]
    assert refused and all(r.context.get("field") == "unknown_key" for r in refused)
    assert key not in repr(world.logger.records)


async def test_chave_desconhecida_com_cara_de_identificador_continua_apontada_pelo_nome(
    monkeypatch: pytest.MonkeyPatch,
):
    """O que o R9 não muda: o erro de digitação legítimo (``promt``) segue dizendo qual é a chave."""
    async with admin_world(monkeypatch) as world:
        response = await world.client.post(f"{BASE}/agents", json=agent_body("a1", promt="x"), headers=ADMIN)

    assert response.json() == {"detail": "Configuração inválida", "field": "promt"}


# ── NaN / Infinity / surrogate solitário ────────────────────────────────────────────────────

_NON_FINITE = [b"NaN", b"Infinity", b"-Infinity", b"1e999"]


def _agent_raw(extra: bytes, agent_id: bytes = b"n1") -> bytes:
    return (
        b'{"id": "' + agent_id + b'", "nome": "n", "factory_ia_model": "ollama", "model": "m", '
        b'"model_params": {"temperature": ' + extra + b"}}"
    )


@pytest.mark.parametrize("literal", _NON_FINITE)
async def test_numero_nao_finito_no_corpo_responde_422_e_nao_envenena_a_listagem(
    monkeypatch: pytest.MonkeyPatch, literal: bytes
):
    async with admin_world(monkeypatch, agents=[agent("a1")]) as world:
        transport = httpx.ASGITransport(app=world.app, client=LOOPBACK_CLIENT, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url=LOOPBACK_BASE_URL) as client:
            created = await client.post(f"{BASE}/agents", content=_agent_raw(literal), headers={**ADMIN, **JSON})
            listed = await client.get(f"{BASE}/agents", headers=ADMIN)

    assert created.status_code == 422
    assert [i.entity.id for i in await world.agents.list_all()] == ["a1"]
    assert listed.status_code == 200


async def test_surrogate_solitario_no_corpo_responde_422(monkeypatch: pytest.MonkeyPatch):
    raw = b'{"id": "s1", "nome": "\\ud800", "factory_ia_model": "ollama", "model": "m"}'
    async with admin_world(monkeypatch) as world:
        transport = httpx.ASGITransport(app=world.app, client=LOOPBACK_CLIENT, raise_app_exceptions=False)
        async with httpx.AsyncClient(transport=transport, base_url=LOOPBACK_BASE_URL) as client:
            created = await client.post(f"{BASE}/agents", content=raw, headers={**ADMIN, **JSON})
            listed = await client.get(f"{BASE}/agents", headers=ADMIN)

    assert created.status_code == 422
    assert listed.status_code == 200


# ── ids hostis no caminho ───────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "raw_id",
    [
        "%7B%22%24ne%22%3A%22%22%7D",
        "%24ne",
        "..%2F..%2Fetc%2Fpasswd",
        "%00",
        "%0d%0aX-Injected%3A1",
        "a" * 5000,
        "%E2%80%AE",
        "id%20com%20espaco",
    ],
)
async def test_id_hostil_no_caminho_e_so_um_id_inexistente(monkeypatch: pytest.MonkeyPatch, raw_id: str):
    async with admin_world(monkeypatch, agents=[agent("a1")]) as world:
        headers = {**ADMIN, "If-Match": '"0"'}
        responses = [
            await world.client.get(f"{BASE}/agents/{raw_id}", headers=ADMIN),
            await world.client.put(f"{BASE}/agents/{raw_id}", json=agent_body("a1"), headers=headers),
            await world.client.delete(f"{BASE}/agents/{raw_id}", headers=headers),
            await world.client.delete(f"{BASE}/agents/{raw_id}", params={"hard": "true"}, headers=headers),
        ]

    assert [r.status_code for r in responses] == [404, 404, 404, 404]
    # "%2F" decodificado vira outro segmento: o roteador responde o 404 dele, sem chegar ao store.
    assert all(r.json()["detail"] in ("Config não encontrada", "Not Found") for r in responses)
    assert [(i.entity.id, i.version) for i in await world.agents.list_all()] == [("a1", 0)]


@pytest.mark.parametrize("bad_id", [123, ["a"], None, "", {"$ne": ""}, True, 1.5])
async def test_id_do_corpo_que_nao_e_texto_responde_422_em_post_e_put(monkeypatch: pytest.MonkeyPatch, bad_id: Any):
    body = agent_body("x")
    body["id"] = bad_id
    async with admin_world(monkeypatch, agents=[agent("a1")]) as world:
        created = await world.client.post(f"{BASE}/agents", json=body, headers=ADMIN)
        replaced = await world.client.put(f"{BASE}/agents/a1", json=body, headers={**ADMIN, "If-Match": '"0"'})

    assert created.status_code == 422
    assert replaced.status_code == 422
    assert [(i.entity.id, i.version) for i in await world.agents.list_all()] == [("a1", 0)]


async def test_texto_que_parece_operador_no_valor_e_dado_comum(monkeypatch: pytest.MonkeyPatch):
    """Só chave ``$`` é operador; o valor ``$ne`` é texto e é gravado como texto."""
    async with admin_world(monkeypatch) as world:
        created = await world.client.post(f"{BASE}/agents", json=agent_body("a1", prompt="$ne"), headers=ADMIN)

    assert created.status_code == 201
    assert (await world.agents.list_all())[0].entity.prompt == "$ne"


# ── falha inesperada do store ───────────────────────────────────────────────────────────────

SECRET_TEXT = "mongodb://usuario:senha-do-banco@host.invalido:27017"  # noqa: S105  # texto de teste, não é credencial


@pytest.mark.parametrize("method", ["GET", "POST", "PUT", "DELETE"])
async def test_falha_inesperada_do_store_responde_500_sem_o_texto_da_excecao(
    monkeypatch: pytest.MonkeyPatch, method: str
):
    async with admin_world(monkeypatch, agents=[agent("a1")]) as world:

        async def boom(*_: object, **__: object) -> None:
            raise RuntimeError(SECRET_TEXT)

        monkeypatch.setattr(world.agents, "list_all", boom)
        transport = httpx.ASGITransport(app=world.app, client=LOOPBACK_CLIENT, raise_app_exceptions=False)
        path = f"{BASE}/agents" if method in ("GET", "POST") else f"{BASE}/agents/a1"
        async with httpx.AsyncClient(transport=transport, base_url=LOOPBACK_BASE_URL) as client:
            response = await client.request(
                method,
                path,
                json=agent_body("a1") if method in ("POST", "PUT") else None,
                headers={**ADMIN, "If-Match": '"0"'},
            )

    assert response.status_code == 500
    assert "senha-do-banco" not in response.text and "RuntimeError" not in response.text


async def test_recusa_nunca_loga_o_corpo_nem_o_id(monkeypatch: pytest.MonkeyPatch):
    marker = "marcador-secreto-do-corpo-9f3a"
    async with admin_world(monkeypatch, agents=[agent("a1")]) as world:
        await world.client.post(f"{BASE}/agents", json=agent_body(marker, tools_ids=[marker]), headers=ADMIN)
        await world.client.post(f"{BASE}/agents", json=agent_body("a1", prompt=marker), headers=ADMIN)
        await world.client.put(f"{BASE}/agents/{marker}", json=agent_body(marker), headers={**ADMIN, "If-Match": '"0"'})
        await world.client.put(
            f"{BASE}/agents/a1", json=agent_body("a1", nome=[marker]), headers={**ADMIN, "If-Match": '"0"'}
        )
        await world.client.delete(f"{BASE}/agents/a1", headers={**ADMIN, "If-Match": '"5"'})

    assert len(world.logger.records) >= 4
    assert marker not in repr(world.logger.records)
