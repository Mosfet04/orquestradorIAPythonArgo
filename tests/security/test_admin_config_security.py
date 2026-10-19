"""FA-02, segurança das rotas ``/admin/config``: authz (só a chave admin), injeção de operador e eco de valor.

Borda real (CORS + auth) com repositórios em memória (``tests/fakes/admin_config_app.py``).
"""

from __future__ import annotations

import pytest

from tests.fakes.admin_config_app import ADMIN, RUN, admin_world, agent, agent_body, tool

BASE = "/admin/config"
ROUTES = [
    ("GET", "/agents"),
    ("POST", "/agents"),
    ("GET", "/agents/a1"),
    ("PUT", "/agents/a1"),
    ("DELETE", "/agents/a1"),
    ("GET", "/teams"),
    ("POST", "/teams"),
    ("GET", "/teams/t1"),
    ("PUT", "/teams/t1"),
    ("DELETE", "/teams/t1"),
    ("GET", "/tools"),
    ("POST", "/tools"),
    ("GET", "/tools/cep"),
    ("PUT", "/tools/cep"),
    ("DELETE", "/tools/cep"),
]
PASTED_VALUE = "sk-valor-de-teste-que-parece-segredo-123456"


@pytest.mark.parametrize(("method", "path"), ROUTES)
async def test_nenhuma_rota_nova_aceita_a_chave_run_nem_request_sem_chave(
    monkeypatch: pytest.MonkeyPatch, method: str, path: str
):
    async with admin_world(monkeypatch, agents=[agent("a1")]) as world:
        with_run = await world.client.request(
            method, BASE + path, json=agent_body("a1"), headers={**RUN, "If-Match": '"0"'}
        )
        anonymous = await world.client.request(method, BASE + path, json=agent_body("a1"))

    assert with_run.status_code == 403
    assert with_run.json() == {"detail": "forbidden"}
    assert anonymous.status_code == 401
    assert [(i.entity.nome, i.version) for i in await world.agents.list_all()] == [("Agente a1", 0)]


@pytest.mark.parametrize(
    "body",
    [
        {**agent_body("a2"), "id": {"$ne": ""}},
        agent_body("a2", tools_ids=[{"$ne": ""}]),
        agent_body("a2", model_params={"$where": "1"}),
        agent_body("a2", rag_config={"active": True, "$set": {"x": 1}}),
        {"$set": {"active": False}},
    ],
)
async def test_corpo_com_operador_mongo_responde_422_sem_gravar(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, object]
):
    async with admin_world(monkeypatch, agents=[agent("a1")]) as world:
        created = await world.client.post(f"{BASE}/agents", json=body, headers=ADMIN)
        replaced = await world.client.put(f"{BASE}/agents/a1", json=body, headers={**ADMIN, "If-Match": '"0"'})

    assert created.status_code == replaced.status_code == 422
    assert "$" not in created.text and "$" not in replaced.text
    assert [(i.entity.id, i.version) for i in await world.agents.list_all()] == [("a1", 0)]


@pytest.mark.parametrize(
    "body",
    [
        agent_body("a2", api_key_ref=PASTED_VALUE),
        agent_body("a2", api_key_ref=f"env:{PASTED_VALUE}"),
        agent_body("a2", base_url=f"https://{PASTED_VALUE}@host.example"),
        agent_body("a2", model_params={"temperature": {"valor": PASTED_VALUE}}),
        agent_body("a2", rag_config={"active": True, "search_strategy": PASTED_VALUE}),
        agent_body("a2", nome={"valor": PASTED_VALUE}),
        agent_body(PASTED_VALUE, tools_ids=[PASTED_VALUE]),
        {"id": "t9", "nome": "T", "factory_ia_model": "o", "model": "m", "member_ids": ["x"], "mode": PASTED_VALUE},
    ],
)
async def test_erro_de_validacao_nao_ecoa_o_valor_nem_o_loga(
    monkeypatch: pytest.MonkeyPatch, body: dict[str, object]
):
    kind = "teams" if "member_ids" in body else "agents"
    async with admin_world(monkeypatch) as world:
        response = await world.client.post(f"{BASE}/{kind}", json=body, headers=ADMIN)

    assert response.status_code == 422
    assert PASTED_VALUE not in response.text
    assert response.json()["detail"] == "Configuração inválida"
    assert PASTED_VALUE not in repr(world.logger.records)
    assert await world.agents.list_all() == []


async def test_tool_com_parametro_invalido_responde_422_sem_eco(monkeypatch: pytest.MonkeyPatch):
    body = {
        "id": "x",
        "name": "x",
        "description": "d",
        "route": "https://api.example.invalid/x",
        "http_method": PASTED_VALUE,
        "parameters": [{"name": "p", "type": PASTED_VALUE, "description": "d"}],
    }
    async with admin_world(monkeypatch) as world:
        response = await world.client.post(f"{BASE}/tools", json=body, headers=ADMIN)

    assert response.status_code == 422
    assert PASTED_VALUE not in response.text


@pytest.mark.parametrize("raw", [b"nao e json", b"[1, 2]", b"\xff\xfe", b"[" * 100_000])
async def test_corpo_que_nao_e_objeto_json_responde_422(monkeypatch: pytest.MonkeyPatch, raw: bytes):
    async with admin_world(monkeypatch) as world:
        response = await world.client.post(
            f"{BASE}/agents", content=raw, headers={**ADMIN, "Content-Type": "application/json"}
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "Configuração inválida"}


async def test_headers_da_tool_nunca_saem_em_texto_em_nenhuma_resposta(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch, tools=[tool("cep", headers={"X-Api-Key": PASTED_VALUE})]) as world:
        etag = {**ADMIN, "If-Match": '"0"'}
        responses = [
            await world.client.get(f"{BASE}/tools", headers=ADMIN),
            await world.client.get(f"{BASE}/tools/cep", headers=ADMIN),
            await world.client.delete(f"{BASE}/tools/cep", headers=etag),
        ]

    assert [r.status_code for r in responses] == [200, 200, 200]
    for response in responses:
        assert PASTED_VALUE not in response.text


async def test_header_mascarado_nao_acompanha_a_tool_para_outro_host(monkeypatch: pytest.MonkeyPatch):
    """Quem tem a chave admin não redireciona o segredo gravado: ``***`` com outra origem na ``route`` é 422."""
    async with admin_world(monkeypatch, tools=[tool("cep", headers={"Authorization": PASTED_VALUE})]) as world:
        shown = (await world.client.get(f"{BASE}/tools/cep", headers=ADMIN)).json()
        moved = {**shown, "route": "https://coletor.example.invalid/{cep}"}
        response = await world.client.put(f"{BASE}/tools/cep", json=moved, headers={**ADMIN, "If-Match": '"0"'})

    assert response.status_code == 422
    assert response.json() == {"detail": "Configuração inválida", "field": "headers"}
    stored = (await world.tools.list_all())[0]
    assert (stored.entity.route, stored.version) == ("https://api.example.invalid/cep/{cep}", 0)
