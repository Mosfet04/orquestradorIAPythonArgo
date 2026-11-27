"""QA do F4-02, segurança na escrita: a API Admin recusa servidor MCP que a conexão recusaria depois.

Guarda real da ``route`` (``route_guard``, DNS falso) e repositórios em memória (``admin_world``).
"""

from __future__ import annotations

import pytest

from src.infrastructure.mcp_client.sessions import schema_hash
from tests.fakes.admin_config_app import ADMIN, admin_world, if_match, route_guard, tool_body
from tests.fakes.mcp_server import mcp_tool

SEARCH = mcp_tool("search", "Busca issues")
PINNED = {"search": schema_hash(SEARCH.name, SEARCH.description, SEARCH.inputSchema)}
PUBLIC = "https://mcp.example.com/mcp"
BASE = "/admin/config/tools"
INVALID = "Configuração inválida"


def _body(**extra: object) -> dict[str, object]:
    fields: dict[str, object] = {"id": "github", "name": "GitHub", "route": PUBLIC, "mcp_tools": PINNED}
    fields.update(extra)
    return tool_body(**fields)


def _public_guard():
    return route_guard(resolver=lambda host: ["93.184.216.34"])


@pytest.mark.parametrize(
    "route",
    [
        "file:///usr/bin/servidor",
        "ws://mcp.example.com/mcp",
        "wss://mcp.example.com/mcp",
        "stdio://python servidor.py",
        "npx -y @modelcontextprotocol/server-github",
        "ftp://mcp.example.com/mcp",
        "http://[::1]/mcp",
        "http://[::ffff:10.0.0.1]/mcp",
        "http://2130706433/mcp",
        "http://user:pass@169.254.169.254/mcp",
    ],
)
async def test_servidor_mcp_com_route_que_nao_e_http_publico_e_recusado_com_422_em_route(monkeypatch, route):
    async with admin_world(monkeypatch, guard=_public_guard()) as world:
        response = await world.client.post(BASE, json=_body(route=route), headers=ADMIN)
        stored = await world.tools.list_all()

    assert (response.status_code, response.json()) == (422, {"detail": INVALID, "field": "route"})
    assert stored == []


async def test_servidor_mcp_que_resolve_para_ip_privado_e_recusado_na_escrita(monkeypatch):
    guard = route_guard(resolver=lambda host: ["10.0.0.7"])
    async with admin_world(monkeypatch, guard=guard) as world:
        response = await world.client.post(BASE, json=_body(route="https://mcp.intranet.example/mcp"), headers=ADMIN)

    assert (response.status_code, response.json()) == (422, {"detail": INVALID, "field": "route"})


async def test_servidor_mcp_com_parameters_e_recusado(monkeypatch):
    parameter = {"name": "q", "type": "string", "description": "busca", "required": True}
    async with admin_world(monkeypatch, guard=_public_guard()) as world:
        response = await world.client.post(BASE, json=_body(parameters=[parameter]), headers=ADMIN)

    assert (response.status_code, response.json()) == (422, {"detail": INVALID, "field": "parameters"})


@pytest.mark.parametrize(
    "mcp_tools",
    [{}, {"a.b": "a" * 64}, {"search": "A" * 64}, {"search": None}, ["search"], "search"],
)
async def test_allowlist_malformada_e_recusada_com_422_em_mcp_tools(monkeypatch, mcp_tools):
    async with admin_world(monkeypatch, guard=_public_guard()) as world:
        response = await world.client.post(BASE, json=_body(mcp_tools=mcp_tools), headers=ADMIN)
        stored = await world.tools.list_all()

    assert response.status_code == 422
    assert response.json()["field"] == "mcp_tools"
    assert stored == []


async def test_header_secreto_literal_no_servidor_mcp_e_recusado_e_nunca_ecoado(monkeypatch):
    literal = "ghp_valor_literal_que_nao_pode_ser_gravado"
    async with admin_world(monkeypatch, guard=_public_guard()) as world:
        response = await world.client.post(
            BASE, json=_body(headers={"Authorization": literal}), headers=ADMIN
        )
        stored = await world.tools.list_all()

    assert response.status_code == 422
    assert response.json()["field"] == "headers"
    assert literal not in response.text
    assert stored == []


async def test_header_por_referencia_so_vai_a_host_do_operador_em_servidor_publico_e_recusado(monkeypatch):
    async with admin_world(monkeypatch, guard=_public_guard()) as world:
        response = await world.client.post(
            BASE, json=_body(headers={"Authorization": "env:GITHUB_API_KEY"}), headers=ADMIN
        )

    assert (response.status_code, response.json()) == (422, {"detail": INVALID, "field": "headers"})


async def test_servidor_mcp_que_resolve_para_metadata_e_recusado_mesmo_na_allowlist_do_operador(monkeypatch):
    guard = route_guard("mcp.example.com", resolver=lambda host: ["169.254.169.254"])
    async with admin_world(monkeypatch, guard=guard) as world:
        response = await world.client.post(BASE, json=_body(), headers=ADMIN)

    assert (response.status_code, response.json()) == (422, {"detail": INVALID, "field": "route"})


async def test_header_por_referencia_e_aceito_em_host_do_operador_e_devolvido_como_referencia(monkeypatch):
    guard = route_guard("mcp.example.com", resolver=lambda host: ["93.184.216.34"])
    async with admin_world(monkeypatch, guard=guard) as world:
        response = await world.client.post(
            BASE, json=_body(headers={"Authorization": "env:GITHUB_API_KEY"}), headers=ADMIN
        )

    assert response.status_code == 201
    assert response.json()["headers"] == {"Authorization": "env:GITHUB_API_KEY"}
    assert response.json()["mcp_tools"] == PINNED


async def test_atualizar_a_route_do_servidor_mcp_para_destino_proibido_e_recusado(monkeypatch):
    async with admin_world(monkeypatch, guard=_public_guard()) as world:
        created = await world.client.post(BASE, json=_body(), headers=ADMIN)
        version = created.json().get("version", 0)
        response = await world.client.put(
            f"{BASE}/github", json=_body(route="http://169.254.169.254/mcp"), headers=if_match(version)
        )
        [stored] = await world.tools.list_all()

    assert created.status_code == 201
    assert (response.status_code, response.json()) == (422, {"detail": INVALID, "field": "route"})
    assert stored.entity.route == PUBLIC


async def test_atualizar_trocando_o_hash_fixado_e_o_caminho_oficial_e_fica_gravado(monkeypatch):
    novo = {"search": "b" * 64}
    async with admin_world(monkeypatch, guard=_public_guard()) as world:
        created = await world.client.post(BASE, json=_body(), headers=ADMIN)
        response = await world.client.put(
            f"{BASE}/github", json=_body(mcp_tools=novo), headers=if_match(created.json().get("version", 0))
        )
        [stored] = await world.tools.list_all()

    assert response.status_code == 200, response.text
    assert stored.entity.mcp_tools == novo


async def test_tool_http_sem_mcp_tools_segue_valida_e_volta_sem_o_campo_preenchido(monkeypatch):
    async with admin_world(monkeypatch, guard=_public_guard()) as world:
        response = await world.client.post(BASE, json=tool_body(route="https://api.example.com/x"), headers=ADMIN)
        [stored] = await world.tools.list_all()

    assert response.status_code == 201
    assert response.json().get("mcp_tools") is None
    assert stored.entity.mcp_tools is None
