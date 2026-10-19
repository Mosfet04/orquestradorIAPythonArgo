"""QA do FA-03: cada valor anunciado em ``/admin/schema`` é aceito pelo POST do FA-02 e um de fora é recusado.

Também: métodos que não são GET, coerência de ``config_store.writable`` com a escrita (Mongo 201, YAML 409),
estado inválido no ``app.state`` e isolamento entre apps.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.fakes.admin_config_app import ADMIN, RUN, admin_world, agent, agent_body

SCHEMA = "/admin/schema"
CONFIG = "/admin/config"


async def _schema(client: Any) -> dict[str, Any]:
    response = await client.get(SCHEMA, headers=ADMIN)
    assert response.status_code == 200
    return response.json()


async def test_cada_modo_de_team_anunciado_e_aceito_no_post_e_um_de_fora_responde_422(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch, agents=[agent("a1")]) as world:
        modes = (await _schema(world.client))["team"]["mode"]
        statuses: dict[str, int] = {}
        for index, mode in enumerate([*modes, "sequential", "ROUTE"]):
            body = {
                "id": f"t{index}", "nome": "T", "factory_ia_model": "ollama", "model": "m",
                "member_ids": ["a1"], "mode": mode,
            }
            reply = await world.client.post(f"{CONFIG}/teams", json=body, headers=ADMIN)
            statuses[mode] = reply.status_code
            if reply.status_code == 422:
                assert reply.json()["field"] == "mode"
                assert mode not in reply.text  # sem eco do valor recusado

    assert [statuses[m] for m in modes] == [201] * len(modes)
    assert statuses["sequential"] == 422 and statuses["ROUTE"] == 422


async def test_cada_metodo_e_tipo_de_parametro_anunciado_e_aceito_no_post_de_tool_e_um_de_fora_responde_422(
    monkeypatch: pytest.MonkeyPatch,
):
    async with admin_world(monkeypatch) as world:
        tool_schema = (await _schema(world.client))["tool"]

        async def post(index: int, method: str, param_type: str) -> Any:
            body = {
                "id": f"x{index}", "name": f"x{index}", "description": "d",
                "route": "https://api.example.invalid/x", "http_method": method,
                "parameters": [{"name": "p", "type": param_type, "description": "d"}],
            }
            return await world.client.post(f"{CONFIG}/tools", json=body, headers=ADMIN)

        index = 0
        for method in tool_schema["http_method"]:
            index += 1
            reply = await post(index, method, "string")
            assert reply.status_code == 201, method
            assert reply.json()["http_method"] == method
        for param_type in tool_schema["parameter_type"]:
            index += 1
            reply = await post(index, "GET", param_type)
            assert reply.status_code == 201, param_type
            assert reply.json()["parameters"][0]["type"] == param_type
        bad_method = await post(900, "TRACE", "string")
        bad_type = await post(901, "GET", "datetime")

    assert (bad_method.status_code, bad_method.json()["field"]) == (422, "http_method")
    assert bad_type.status_code == 422
    assert "datetime" not in bad_type.text and "TRACE" not in bad_method.text


async def test_cada_search_strategy_anunciada_e_aceita_no_post_de_agent_e_uma_de_fora_responde_422(
    monkeypatch: pytest.MonkeyPatch,
):
    async with admin_world(monkeypatch) as world:
        strategies = (await _schema(world.client))["rag_config"]["search_strategy"]
        accepted = []
        for index, strategy in enumerate(strategies):
            rag = {"active": True, "doc_name": "d", "search_strategy": strategy}
            reply = await world.client.post(
                f"{CONFIG}/agents", json=agent_body(f"r{index}", rag_config=rag), headers=ADMIN
            )
            accepted.append((reply.status_code, reply.json().get("rag_config", {}).get("search_strategy")))
        refused = await world.client.post(
            f"{CONFIG}/agents",
            json=agent_body("r9", rag_config={"active": True, "search_strategy": "fuzzy"}),
            headers=ADMIN,
        )

    assert accepted == [(201, strategy) for strategy in strategies]
    assert (refused.status_code, refused.json()["field"]) == (422, "rag_config.search_strategy")
    assert "fuzzy" not in refused.text


async def test_formato_de_api_key_ref_anunciado_preenchido_e_aceito_no_post_e_o_fora_do_formato_responde_422(
    monkeypatch: pytest.MonkeyPatch,
):
    async with admin_world(monkeypatch) as world:
        formats = (await _schema(world.client))["api_key_ref"]["formats"]
        filled = [
            f.replace("<PROVEDOR>", "ACME").replace("<caminho em SECRETS_DIR>", "/run/secrets/acme") for f in formats
        ]
        ok = [
            (await world.client.post(
                f"{CONFIG}/agents", json=agent_body(f"k{i}", api_key_ref=ref), headers=ADMIN
            )).status_code
            for i, ref in enumerate(filled)
        ]
        hostile = {
            "env:API_KEY_ADMIN": "k-admin", "env:acme_api_key": "k-lower", "env:ACME_KEY": "k-nosuffix",
            "ACME_API_KEY": "k-bare", "http://evil.example/x": "k-url",
        }
        refused = {
            ref: await world.client.post(f"{CONFIG}/agents", json=agent_body(name, api_key_ref=ref), headers=ADMIN)
            for ref, name in hostile.items()
        }

    assert ok == [201] * len(formats)
    for ref, reply in refused.items():
        assert (ref, reply.status_code, reply.json()["field"]) == (ref, 422, "api_key_ref")
        assert ref not in reply.text


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
async def test_metodo_diferente_de_get_nao_altera_estado_e_responde_405_para_admin(
    monkeypatch: pytest.MonkeyPatch, method: str
):
    async with admin_world(monkeypatch) as world:
        before = await _schema(world.client)
        denied = await world.client.request(method, SCHEMA, headers=ADMIN, json={"config_store": {"writable": False}})
        after = await _schema(world.client)

    assert denied.status_code == 405
    assert "allow" in denied.headers and "GET" in denied.headers["allow"]
    assert before == after


@pytest.mark.parametrize("method", ["POST", "PUT", "DELETE", "PATCH"])
async def test_metodo_diferente_de_get_com_chave_run_responde_403_e_sem_chave_401(
    monkeypatch: pytest.MonkeyPatch, method: str
):
    async with admin_world(monkeypatch) as world:
        with_run = await world.client.request(method, SCHEMA, headers=RUN)
        anonymous = await world.client.request(method, SCHEMA)

    assert (with_run.status_code, anonymous.status_code) == (403, 401)


async def test_query_string_e_barra_final_nao_geram_erro_nem_mudam_o_corpo(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch) as world:
        plain = await world.client.get(SCHEMA, headers=ADMIN)
        with_query = await world.client.get(SCHEMA, params={"kind": "chat"}, headers=ADMIN)
        slash = await world.client.get(SCHEMA + "/", headers=ADMIN)
        run_slash = await world.client.get(SCHEMA + "/", headers=RUN)

    assert with_query.status_code == 200 and with_query.json() == plain.json()
    assert slash.status_code == 307
    assert run_slash.status_code == 403
    assert plain.headers["content-type"].startswith("application/json")


@pytest.mark.parametrize("bad", [None, {"config_store": {"writable": True}}, "schema"], ids=["none", "dict", "str"])
async def test_estado_com_tipo_errado_responde_503_sem_vazar_o_conteudo(monkeypatch: pytest.MonkeyPatch, bad: object):
    async with admin_world(monkeypatch) as world:
        world.app.state.config_schema = bad
        response = await world.client.get(SCHEMA, headers=ADMIN)
        with_run = await world.client.get(SCHEMA, headers=RUN)

    assert response.status_code == 503
    assert response.json() == {"detail": "Schema da configuração indisponível"}
    assert with_run.status_code == 403
