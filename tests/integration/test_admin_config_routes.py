"""FA-02: CRUD ``/admin/config/{agents,teams,tools}`` pela borda real (auth + CORS) com repositórios em memória.

Concorrência otimista pelo ETag (= ``version``), integridade referencial, soft/hard delete, paginação e
headers de tool mascarados. A pilha completa com o container e o store YAML fica no último teste.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from src.domain.entities.agent_config import AgentConfig
from src.domain.repositories.versioning import ConfigVersionConflict
from src.infrastructure.dependency_injection import build_config_admin
from src.presentation.routes.admin_config import router as admin_config_router
from tests.fakes.admin_config_app import ADMIN, admin_world, agent, agent_body, team, tool
from tests.fakes.repositories import InMemoryAgentConfigRepository
from tests.fakes.yaml_world import ADMIN as YAML_ADMIN
from tests.fakes.yaml_world import yaml_app

BASE = "/admin/config"
TEAM_BODY = {"id": "t1", "nome": "T", "factory_ia_model": "ollama", "model": "qwen3", "member_ids": ["a1"]}
MIB = 1024 * 1024


def _if_match(version: int) -> dict[str, str]:
    return {**ADMIN, "If-Match": f'"{version}"'}


async def test_post_cria_e_get_devolve_o_documento_com_etag_igual_a_versao(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch) as world:
        created = await world.client.post(f"{BASE}/agents", json=agent_body("a1"), headers=ADMIN)
        fetched = await world.client.get(f"{BASE}/agents/a1", headers=ADMIN)

    assert created.status_code == 201
    assert created.headers["etag"] == '"1"'
    assert created.json()["id"] == "a1" and created.json()["version"] == 1
    assert fetched.status_code == 200
    assert fetched.headers["etag"] == '"1"'
    assert fetched.json()["nome"] == "Agente a1"
    assert (await world.agents.list_all())[0].version == 1


async def test_post_de_id_existente_responde_409_sem_tocar_o_documento(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch, agents=[agent("a1")]) as world:
        response = await world.client.post(f"{BASE}/agents", json=agent_body("a1", nome="Outro"), headers=ADMIN)

    assert response.status_code == 409
    stored = await world.agents.list_all()
    assert [(item.entity.nome, item.version) for item in stored] == [("Agente a1", 0)]


async def test_get_lista_paginada_inclui_inativos(monkeypatch: pytest.MonkeyPatch):
    seeded = [agent("a1"), agent("a2", active=False), agent("a3")]
    async with admin_world(monkeypatch, agents=seeded) as world:
        page = await world.client.get(f"{BASE}/agents", params={"offset": 1, "limit": 1}, headers=ADMIN)
        everything = await world.client.get(f"{BASE}/agents", headers=ADMIN)
        bad = await world.client.get(f"{BASE}/agents", params={"limit": "muitos"}, headers=ADMIN)

    assert page.status_code == 200
    assert page.json()["total"] == 3
    assert [(item["id"], item["active"], item["version"]) for item in page.json()["items"]] == [("a2", False, 0)]
    assert [item["id"] for item in everything.json()["items"]] == ["a1", "a2", "a3"]
    assert bad.status_code == 422
    assert "muitos" not in bad.text


async def test_get_de_id_inexistente_responde_404(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch) as world:
        responses = [
            await world.client.get(f"{BASE}/tools/nada", headers=ADMIN),
            await world.client.put(f"{BASE}/tools/nada", json={}, headers=_if_match(0)),
            await world.client.delete(f"{BASE}/tools/nada", headers=_if_match(0)),
            await world.client.delete(f"{BASE}/tools/nada", params={"hard": "true"}, headers=_if_match(0)),
        ]

    assert [r.status_code for r in responses] == [404, 404, 404, 404]


async def test_put_sem_if_match_428_divergente_412_e_certo_grava_a_proxima_versao(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch, agents=[agent("a1")]) as world:
        body = agent_body("a1", prompt="novo prompt")
        missing = await world.client.put(f"{BASE}/agents/a1", json=body, headers=ADMIN)
        stale = await world.client.put(f"{BASE}/agents/a1", json=body, headers=_if_match(7))
        garbage = await world.client.put(f"{BASE}/agents/a1", json=body, headers={**ADMIN, "If-Match": "abc"})
        ok = await world.client.put(f"{BASE}/agents/a1", json=body, headers=_if_match(0))
        again = await world.client.put(f"{BASE}/agents/a1", json=body, headers=_if_match(0))

    assert (missing.status_code, stale.status_code, garbage.status_code) == (428, 412, 412)
    assert ok.status_code == 200
    assert ok.headers["etag"] == '"1"'
    assert ok.json()["prompt"] == "novo prompt"
    assert again.status_code == 412
    assert [(i.entity.prompt, i.version) for i in await world.agents.list_all()] == [("novo prompt", 1)]


async def test_put_com_id_do_corpo_diferente_da_rota_responde_422(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch, agents=[agent("a1")]) as world:
        response = await world.client.put(f"{BASE}/agents/a1", json=agent_body("a2"), headers=_if_match(0))

    assert response.status_code == 422
    assert response.json()["field"] == "id"


async def test_tools_ids_e_member_ids_inexistentes_respondem_422_com_o_campo(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch, agents=[agent("a1")], tools=[tool("cep")]) as world:
        bad_agent = await world.client.post(
            f"{BASE}/agents", json=agent_body("a2", tools_ids=["cep", "fantasma"]), headers=ADMIN
        )
        bad_team = await world.client.post(
            f"{BASE}/teams",
            json={**TEAM_BODY, "member_ids": ["fantasma", "a1"]},
            headers=ADMIN,
        )
        good_team = await world.client.post(f"{BASE}/teams", json=TEAM_BODY, headers=ADMIN)

    assert bad_agent.status_code == 422
    assert bad_agent.json() == {"detail": "Configuração inválida", "field": "tools_ids", "indexes": [1]}
    assert bad_team.status_code == 422
    assert bad_team.json() == {"detail": "Configuração inválida", "field": "member_ids", "indexes": [0]}
    assert good_team.status_code == 201


async def test_erro_de_validacao_responde_422_com_o_campo_sem_o_valor(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch, agents=[agent("a1")]) as world:
        bad_name = await world.client.post(f"{BASE}/agents", json=agent_body("a2", nome=["x"]), headers=ADMIN)
        bad_mode = await world.client.post(f"{BASE}/teams", json={**TEAM_BODY, "mode": "valor-x"}, headers=ADMIN)
        no_provider = await world.client.post(
            f"{BASE}/teams", json={k: v for k, v in TEAM_BODY.items() if k != "factory_ia_model"}, headers=ADMIN
        )
        bad_list = await world.client.post(f"{BASE}/agents", json=agent_body("a2", tools_ids="cep"), headers=ADMIN)

    assert bad_name.status_code == 422
    assert bad_name.json() == {"detail": "Configuração inválida", "field": "nome"}
    assert bad_mode.json() == {"detail": "Configuração inválida", "field": "mode"}
    assert no_provider.json() == {"detail": "Configuração inválida", "field": "factory_ia_model"}
    assert bad_list.json() == {"detail": "Configuração inválida", "field": "tools_ids"}
    assert [i.entity.id for i in await world.agents.list_all()] == ["a1"]
    assert await world.teams.list_all() == []


def _nested(depth: int) -> object:
    value: object = "folha"
    for _ in range(depth):
        value = {"x": value}
    return value


async def test_corpo_aninhado_demais_responde_422_e_nao_500(monkeypatch: pytest.MonkeyPatch):
    deep_tool = {
        "id": "cep",
        "name": "cep",
        "description": "d",
        "route": "https://api.example.invalid/cep",
        "parameters": [{"name": "p", "type": "object", "description": "d", "default_value": _nested(500)}],
    }
    async with admin_world(monkeypatch) as world:
        deep_prompt = await world.client.post(
            f"{BASE}/agents", json=agent_body("a1", prompt=_nested(500)), headers=ADMIN
        )
        deep_default = await world.client.post(f"{BASE}/tools", json=deep_tool, headers=ADMIN)

    assert (deep_prompt.status_code, deep_default.status_code) == (422, 422)
    assert await world.agents.list_all() == [] and await world.tools.list_all() == []


async def _chunks(size: int) -> AsyncIterator[bytes]:
    yield b'{"id": "a1", "prompt": "'
    for _ in range(size // (64 * 1024)):
        yield b"x" * (64 * 1024)
    yield b'"}'


async def test_corpo_acima_do_teto_responde_413_com_ou_sem_content_length(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch, agents=[agent("a1")]) as world:
        declared = await world.client.post(f"{BASE}/agents", json=agent_body("a2", prompt="x" * MIB), headers=ADMIN)
        chunked = await world.client.put(
            f"{BASE}/agents/a1", content=_chunks(2 * MIB), headers={**_if_match(0), "Content-Type": "application/json"}
        )
        just_below = await world.client.post(
            f"{BASE}/agents", json=agent_body("a3", prompt="x" * (MIB - 1024)), headers=ADMIN
        )

    assert "content-length" not in chunked.request.headers
    assert (declared.status_code, chunked.status_code, just_below.status_code) == (413, 413, 201)
    assert [(i.entity.id, i.version) for i in await world.agents.list_all()] == [("a1", 0), ("a3", 1)]


async def test_remover_tool_ou_agente_em_uso_responde_409_com_quem_usa(monkeypatch: pytest.MonkeyPatch):
    seeded_agents = [agent("a1", tools_ids=["cep"]), agent("a2", tools_ids=["cep"], active=False)]
    async with admin_world(
        monkeypatch, agents=seeded_agents, teams=[team("t1", ["a1"])], tools=[tool("cep")]
    ) as world:
        tool_soft = await world.client.delete(f"{BASE}/tools/cep", headers=_if_match(0))
        tool_hard = await world.client.delete(f"{BASE}/tools/cep", params={"hard": "true"}, headers=_if_match(0))
        agent_hard = await world.client.delete(f"{BASE}/agents/a1", params={"hard": "true"}, headers=_if_match(0))

    assert tool_soft.status_code == tool_hard.status_code == 409
    assert tool_hard.json() == {"detail": "Config em uso", "used_by": {"agents": ["a1", "a2"]}}
    assert agent_hard.status_code == 409
    assert agent_hard.json()["used_by"] == {"teams": ["t1"]}
    assert len(await world.tools.list_all()) == 1


async def test_delete_soft_desativa_e_hard_remove(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch, agents=[agent("a1"), agent("a2")]) as world:
        missing = await world.client.delete(f"{BASE}/agents/a1", headers=ADMIN)
        soft = await world.client.delete(f"{BASE}/agents/a1", headers=_if_match(0))
        hard_stale = await world.client.delete(f"{BASE}/agents/a2", params={"hard": "true"}, headers=_if_match(3))
        hard = await world.client.delete(f"{BASE}/agents/a2", params={"hard": "true"}, headers=_if_match(0))
        bad_flag = await world.client.delete(f"{BASE}/agents/a1", params={"hard": "sim"}, headers=_if_match(1))

    assert missing.status_code == 428
    assert soft.status_code == 200
    assert soft.headers["etag"] == '"1"'
    assert soft.json()["active"] is False
    assert hard_stale.status_code == 412
    assert hard.status_code == 204
    assert bad_flag.status_code == 422
    assert [(i.entity.id, i.entity.active, i.version) for i in await world.agents.list_all()] == [("a1", False, 1)]


async def test_get_de_tool_mascara_headers_e_put_com_mascara_mantem_o_valor_gravado(monkeypatch: pytest.MonkeyPatch):
    header_value = "Bearer valor-de-teste-do-header"
    async with admin_world(monkeypatch, tools=[tool("cep", headers={"Authorization": header_value})]) as world:
        listed = await world.client.get(f"{BASE}/tools", headers=ADMIN)
        fetched = await world.client.get(f"{BASE}/tools/cep", headers=ADMIN)
        body = {**fetched.json(), "description": "nova descrição"}
        updated = await world.client.put(f"{BASE}/tools/cep", json=body, headers=_if_match(0))
        unknown = await world.client.put(
            f"{BASE}/tools/cep", json={**body, "headers": {"X-Outro": "***"}}, headers=_if_match(1)
        )

    assert fetched.json()["headers"] == {"Authorization": "***"}
    assert listed.json()["items"][0]["headers"] == {"Authorization": "***"}
    assert updated.status_code == 200
    assert updated.json()["headers"] == {"Authorization": "***"}
    assert unknown.status_code == 422
    assert unknown.json()["field"] == "headers"
    for response in (listed, fetched, updated, unknown):
        assert header_value not in response.text
    stored = (await world.tools.list_all())[0]
    assert (stored.entity.description, stored.entity.headers) == ("nova descrição", {"Authorization": header_value})


async def test_tool_criada_com_header_em_texto_grava_o_valor_e_devolve_mascarado(monkeypatch: pytest.MonkeyPatch):
    body = {
        "id": "clima",
        "name": "clima",
        "description": "Previsão do tempo",
        "route": "https://api.example.invalid/clima",
        "http_method": "get",
        "headers": {"X-Api-Key": "valor-de-teste"},
    }
    async with admin_world(monkeypatch) as world:
        created = await world.client.post(f"{BASE}/tools", json=body, headers=ADMIN)

    assert created.status_code == 201
    assert (created.json()["headers"], created.json()["http_method"]) == ({"X-Api-Key": "***"}, "GET")
    assert (await world.tools.list_all())[0].entity.headers == {"X-Api-Key": "valor-de-teste"}


async def test_team_nao_e_referenciado_e_sai_com_soft_e_hard_delete(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch, agents=[agent("a1")], teams=[team("t1", ["a1"])]) as world:
        soft = await world.client.delete(f"{BASE}/teams/t1", headers=_if_match(0))
        hard = await world.client.delete(f"{BASE}/teams/t1", params={"hard": "true"}, headers=_if_match(1))

    assert (soft.status_code, soft.json()["active"], hard.status_code) == (200, False, 204)
    assert await world.teams.list_all() == []


async def test_sem_o_caso_de_uso_no_estado_do_app_responde_503(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch) as world:
        del world.app.state.config_admin
        response = await world.client.get(f"{BASE}/agents", headers=ADMIN)

    assert response.status_code == 503


@pytest.mark.parametrize(
    ("extra", "field"),
    [
        ({"apiKeyRef": "env:OPENAI_API_KEY"}, "apiKeyRef"),
        ({"baseUrl": "https://x.example"}, "baseUrl"),
        ({"promt": "prompt digitado errado"}, "promt"),
        ({"rag_config": {"active": True, "modelParams": {"dimensions": 8}}}, "rag_config.modelParams"),
    ],
)
async def test_chave_desconhecida_ou_camel_case_responde_422_com_a_chave_sem_gravar(
    monkeypatch: pytest.MonkeyPatch, extra: dict[str, object], field: str
):
    """A escrita estrita não descarta em silêncio o que o mapper não lê (``apiKeyRef`` gravaria sem chave)."""
    async with admin_world(monkeypatch) as world:
        response = await world.client.post(f"{BASE}/agents", json=agent_body("a1", **extra), headers=ADMIN)

    assert response.status_code == 422
    assert response.json() == {"detail": "Configuração inválida", "field": field}
    assert await world.agents.list_all() == []


async def test_get_e_put_do_mesmo_documento_regrava_sem_mudar_nada(monkeypatch: pytest.MonkeyPatch):
    """O ``version`` que o GET mostra volta no corpo do PUT (a UI reenvia o documento lido) e é aceito."""
    rag = {"active": True, "doc_name": "d", "model": "m", "factory_ia_model": "openai", "base_url": "https://e.example"}
    async with admin_world(monkeypatch, tools=[tool("cep")]) as world:
        created = await world.client.post(
            f"{BASE}/agents", json=agent_body("a1", tools_ids=["cep"], rag_config=rag), headers=ADMIN
        )
        await world.client.post(f"{BASE}/teams", json=TEAM_BODY, headers=ADMIN)
        replies = {}
        for kind, entity_id in (("agents", "a1"), ("teams", "t1"), ("tools", "cep")):
            fetched = await world.client.get(f"{BASE}/{kind}/{entity_id}", headers=ADMIN)
            if_match = {**ADMIN, "If-Match": fetched.headers["etag"]}
            replaced = await world.client.put(f"{BASE}/{kind}/{entity_id}", json=fetched.json(), headers=if_match)
            replies[kind] = (fetched, replaced)

    assert created.status_code == 201
    for fetched, replaced in replies.values():
        assert replaced.status_code == 200
        assert replaced.json() == {**fetched.json(), "version": fetched.json()["version"] + 1}


class _StaleSave(ConfigVersionConflict):
    """Subclasse de uma recusa conhecida: casa no ``except`` e precisa casar na tabela de status."""


class _StaleAgents(InMemoryAgentConfigRepository):
    async def save(self, entity: AgentConfig, expected_version: int) -> int:
        raise _StaleSave(entity.id, expected_version)


async def test_subclasse_de_recusa_responde_o_status_da_recusa_e_nao_500(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch) as world:
        stale = _StaleAgents([agent("a1")])
        world.app.state.config_admin = build_config_admin(stale, world.teams, world.tools, world.logger)
        response = await world.client.put(f"{BASE}/agents/a1", json=agent_body("a1"), headers=_if_match(0))

    assert response.status_code == 412
    assert response.json() == {"detail": "Versão divergente: releia a config"}


async def test_rota_de_tipo_desconhecido_responde_404(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch) as world:
        response = await world.client.get(f"{BASE}/segredos", headers=ADMIN)

    assert response.status_code == 404


YAML_CONFIG = """
agents:
  - {id: y1, nome: Agente do YAML, factoryIaModel: ollama, model: m-y1, prompt: p, active: true}
"""


@pytest.mark.usefixtures("offline_knowledge")
def test_pilha_completa_com_store_yaml_lista_pelo_container_e_recusa_escrita_com_409(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    with yaml_app(monkeypatch, tmp_path, YAML_CONFIG, routers=[admin_config_router]) as (_, path, http):
        before = path.read_bytes()
        listed = http.get(f"{BASE}/agents", headers=YAML_ADMIN)
        created = http.post(f"{BASE}/agents", json=agent_body("novo"), headers=YAML_ADMIN)
        updated = http.put(
            f"{BASE}/agents/y1", json=agent_body("y1"), headers={**YAML_ADMIN, "If-Match": '"0"'}
        )
        removed = http.delete(f"{BASE}/agents/y1", params={"hard": "true"}, headers={**YAML_ADMIN, "If-Match": "0"})

    assert listed.status_code == 200
    assert [(item["id"], item["version"]) for item in listed.json()["items"]] == [("y1", 0)]
    assert (created.status_code, updated.status_code, removed.status_code) == (409, 409, 409)
    assert created.json() == {"detail": "O store de configuração é somente leitura"}
    assert path.read_bytes() == before
