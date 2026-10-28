"""QA do FA-02: bordas do CRUD ``/admin/config`` que os testes do dev não cobrem.

Legado camelCase lido e regravado pela API (repositórios Mongo reais sobre coleção fake), paginação nas
bordas, ``If-Match`` com aspas/espaços, corpo de exatamente 1 MiB e 1 MiB + 1, escritas concorrentes sob
a trava (com repositório que cede o loop), integridade referencial com inativos, hard delete dos três
tipos, máscara ``***`` com troca de origem e o lifespan com o container real e o store Mongo.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest
from bson import ObjectId

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.team_config import TeamConfig
from src.domain.entities.tool import Tool
from src.infrastructure.repositories import mongo_base
from src.infrastructure.web import app_factory
from src.presentation.routes.admin_config import router as admin_config_router
from tests.fakes.admin_config_app import (
    ADMIN,
    RUN_KEY,
    admin_env,
    admin_world,
    agent,
    agent_body,
    fake_config_admin,
    if_match,
    team,
    tool,
)
from tests.fakes.config_stores import MongoBackend
from tests.fakes.logger import RecordingLogger
from tests.fakes.repositories import (
    InMemoryAgentConfigRepository,
    InMemoryTeamConfigRepository,
    InMemoryToolRepository,
)
from tests.fakes.startup_world import Events, assign_seed_ids, startup_world
from tests.fakes.web import loopback_client

BASE = "/admin/config"
MIB = 1024 * 1024
JSON = {"Content-Type": "application/json"}


# ── documentos legados (camelCase, sem version) lidos e regravados pela API ──────────────────

LEGACY_AGENT = {
    "id": "a1",
    "nome": "Legado",
    "factoryIaModel": "openai",
    "model": "gpt-x",
    "descricao": "d",
    "prompt": "p",
    "active": True,
    "tools_ids": ["cep"],
}
LEGACY_TEAM = {
    "id": "t1",
    "nome": "Time",
    "factoryIaModel": "ollama",
    "model": "q",
    "memberIds": ["a1"],
    "userMemoryActive": False,
    "summaryActive": True,
}
LEGACY_TOOL = {
    "id": "cep",
    "name": "cep",
    "description": "d",
    "route": "https://api.example.invalid/cep/{cep}",
    "http_method": "get",
    "parameters": [{"name": "cep", "type": "string", "description": "c"}],
    "headers": {"Authorization": "Bearer valor-legado-de-teste"},
}


def _mongo_world(monkeypatch: pytest.MonkeyPatch):
    """Repositórios Mongo reais (coleção fake) com os três documentos legados; devolve também o backend."""
    backend = MongoBackend(monkeypatch)
    logger = RecordingLogger()
    agents = backend.agents([LEGACY_AGENT], logger)
    teams = backend.teams([LEGACY_TEAM], logger)
    tools = backend.tools([LEGACY_TOOL], logger)
    return backend, agents, teams, tools, logger


async def test_documentos_legados_camel_case_sao_lidos_pela_api_e_regravados_em_snake_case_no_mesmo_id(
    monkeypatch: pytest.MonkeyPatch,
):
    backend, agents, teams, tools, logger = _mongo_world(monkeypatch)
    original_ids = {name: col.docs[0]["_id"] for name, col in backend.collections.items()}
    async with admin_world(monkeypatch) as world:
        world.app.state.config_admin = fake_config_admin(agents, teams, tools, logger)
        got = {
            k: await world.client.get(f"{BASE}/{k}/{i}", headers=ADMIN)
            for k, i in (("agents", "a1"), ("teams", "t1"), ("tools", "cep"))
        }
        replies = {}
        for kind, response in got.items():
            body = response.json()
            replies[kind] = await world.client.put(
                f"{BASE}/{kind}/{body['id']}", json=body, headers={**ADMIN, "If-Match": response.headers["etag"]}
            )

    assert got["agents"].json()["factory_ia_model"] == "openai" and got["agents"].json()["version"] == 0
    assert got["teams"].json()["member_ids"] == ["a1"] and got["teams"].json()["summary_active"] is True
    assert got["teams"].json()["user_memory_active"] is False
    assert got["tools"].json()["headers"] == {"Authorization": "***"}
    assert {k: r.status_code for k, r in replies.items()} == {"agents": 200, "teams": 200, "tools": 200}
    for name, col in backend.collections.items():
        stored = col.docs[0]
        assert len(col.docs) == 1 and stored["_id"] == original_ids[name] and stored["version"] == 1
        assert not {"factoryIaModel", "memberIds", "userMemoryActive", "summaryActive"} & set(stored)
    assert backend.collections["teams_config"].docs[0]["member_ids"] == ["a1"]
    assert backend.collections["teams_config"].docs[0]["user_memory_active"] is False
    # o segredo gravado no legado sobrevive ao ciclo GET -> PUT com ***
    assert backend.collections["tools"].docs[0]["headers"] == {"Authorization": "Bearer valor-legado-de-teste"}


async def test_documento_legado_invalido_no_store_nao_derruba_a_listagem_da_api(monkeypatch: pytest.MonkeyPatch):
    backend, agents, teams, tools, logger = _mongo_world(monkeypatch)
    backend.collections["agents_config"].docs.append(
        {"_id": ObjectId(), "id": "ruim", "nome": ["lista"], "factoryIaModel": "o", "model": "m"}
    )
    async with admin_world(monkeypatch) as world:
        world.app.state.config_admin = fake_config_admin(agents, teams, tools, logger)
        listed = await world.client.get(f"{BASE}/agents", headers=ADMIN)
        missing = await world.client.get(f"{BASE}/agents/ruim", headers=ADMIN)

    assert listed.status_code == 200 and [i["id"] for i in listed.json()["items"]] == ["a1"]
    assert missing.status_code == 404


async def test_hard_delete_pelo_mongo_remove_o_documento_e_remover_o_usuario_libera_o_usado(
    monkeypatch: pytest.MonkeyPatch,
):
    backend, agents, teams, tools, logger = _mongo_world(monkeypatch)
    order = [("tools", "cep", 1), ("agents", "a1", 1), ("teams", "t1", 0)]
    async with admin_world(monkeypatch) as world:
        world.app.state.config_admin = fake_config_admin(agents, teams, tools, logger)
        blocked = [
            await world.client.delete(f"{BASE}/{k}/{i}", params={"hard": "true"}, headers=if_match(0))
            for k, i, _ in order[:2]
        ]
        done = []
        for kind, entity_id, _ in reversed(order):
            done.append(
                await world.client.delete(f"{BASE}/{kind}/{entity_id}", params={"hard": "true"}, headers=if_match(0))
            )
        after = await world.client.get(f"{BASE}/tools/cep", headers=ADMIN)

    assert [r.status_code for r in blocked] == [409, 409]
    assert blocked[0].json()["used_by"] == {"agents": ["a1"]}
    assert blocked[1].json()["used_by"] == {"teams": ["t1"]}
    assert [r.status_code for r in done] == [204, 204, 204]
    assert all(col.docs == [] for col in backend.collections.values())
    assert after.status_code == 404


# ── paginação nas bordas ────────────────────────────────────────────────────────────────────


async def test_paginacao_nas_bordas(monkeypatch: pytest.MonkeyPatch):
    seeded = [agent(f"a{n}") for n in range(5)]
    async with admin_world(monkeypatch, agents=seeded) as world:

        async def page(**params: Any):
            return await world.client.get(f"{BASE}/agents", params=params, headers=ADMIN)

        past_end = await page(offset=5)
        far = await page(offset=1_000_000_000)
        last = await page(offset=4, limit=200)
        exact = await page(offset=0, limit=5)
        limit_max = await page(limit=200)
        invalid = {
            name: await page(**params)
            for name, params in {
                "limit0": {"limit": 0},
                "limit201": {"limit": 201},
                "neg": {"offset": -1},
                "float": {"offset": "1.5"},
                "plus": {"offset": "+1"},
                "space": {"limit": " 1"},
                "unicode": {"limit": "\uff11"},
                "empty": {"limit": ""},
                "huge": {"offset": 10**12},
                "hex": {"limit": "0x10"},
            }.items()
        }
    assert (past_end.status_code, past_end.json()["items"], past_end.json()["total"]) == (200, [], 5)
    assert far.status_code == 200 and far.json()["items"] == []
    assert [i["id"] for i in last.json()["items"]] == ["a4"]
    assert len(exact.json()["items"]) == 5 and limit_max.json()["limit"] == 200
    assert {name: r.status_code for name, r in invalid.items()} == dict.fromkeys(invalid, 422)
    assert all("1.5" not in r.text and "0x10" not in r.text for r in invalid.values())


async def test_listagem_de_store_vazio_e_total_zero(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch) as world:
        responses = [await world.client.get(f"{BASE}/{k}", headers=ADMIN) for k in ("agents", "teams", "tools")]

    assert [r.json() for r in responses] == [{"items": [], "total": 0, "offset": 0, "limit": 50}] * 3


# ── If-Match ────────────────────────────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("header", "status"),
    [
        ('"0"', 200),
        ("0", 200),
        (' "0" ', 200),
        ("  0  ", 200),
        ('W/"0"', 412),
        ("*", 412),
        ('"0", "1"', 412),
        ("", 412),
        ('""', 412),
        ('"', 412),
        ("-0", 412),
        ("+0", 412),
        ("0.0", 412),
        ("9" * 19, 412),
        ("9" * 5000, 412),
        ('"abc"', 412),
    ],
)
async def test_if_match_com_aspas_espacos_e_lixo(monkeypatch: pytest.MonkeyPatch, header: str, status: int):
    async with admin_world(monkeypatch, agents=[agent("a1"), agent("a2")]) as world:
        put = await world.client.put(
            f"{BASE}/agents/a1", json=agent_body("a1", prompt="novo"), headers={**ADMIN, "If-Match": header}
        )
        delete = await world.client.delete(f"{BASE}/agents/a2", headers={**ADMIN, "If-Match": header})

    assert (put.status_code, delete.status_code) == (status, status)
    if status == 412:
        assert put.json() == {"detail": "Versão divergente: releia a config"}
        assert all(i.version == 0 for i in await world.agents.list_all())


async def test_if_match_nao_ascii_nao_derruba_o_servidor(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch, agents=[agent("a1")]) as world:
        response = await world.client.put(
            f"{BASE}/agents/a1",
            json=agent_body("a1"),
            headers=[(b"authorization", ADMIN["Authorization"].encode()), (b"if-match", '"\u0660"'.encode())],
        )

    assert response.status_code == 412


# ── corpo no teto ───────────────────────────────────────────────────────────────────────────


def _body_of_exact_size(size: int) -> bytes:
    head = b'{"id": "a9", "nome": "n", "factory_ia_model": "ollama", "model": "m", "prompt": "'
    tail = b'"}'
    return head + b"x" * (size - len(head) - len(tail)) + tail


async def test_corpo_de_exatamente_1_mib_passa_e_com_1_byte_a_mais_responde_413(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch) as world:
        exact = await world.client.post(f"{BASE}/agents", content=_body_of_exact_size(MIB), headers={**ADMIN, **JSON})
        over = await world.client.post(
            f"{BASE}/agents", content=_body_of_exact_size(MIB + 1).replace(b"a9", b"b9"), headers={**ADMIN, **JSON}
        )

    assert len(_body_of_exact_size(MIB)) == MIB
    assert (exact.status_code, over.status_code) == (201, 413)
    assert [i.entity.id for i in await world.agents.list_all()] == ["a9"]
    assert "Corpo maior" in over.text and "a9" not in over.text


async def test_413_no_put_nao_grava(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch, agents=[agent("a1")]) as world:
        put = await world.client.put(
            f"{BASE}/agents/a1", content=_body_of_exact_size(MIB + 1), headers={**if_match(0), **JSON}
        )

    assert put.status_code == 413
    assert [(i.entity.prompt, i.version) for i in await world.agents.list_all()] == [("p", 0)]


# ── ciclo GET -> PUT com documentos ricos ───────────────────────────────────────────────────


async def test_ciclo_get_put_de_tool_com_parametros_post_e_headers_nao_muda_nada(monkeypatch: pytest.MonkeyPatch):
    body = {
        "id": "busca",
        "name": "busca",
        "description": "d",
        "route": "https://api.example.invalid/busca",
        "http_method": "post",
        "instructions": "use com cuidado",
        "parameters": [
            {"name": "q", "type": "string", "description": "consulta", "required": True},
            {"name": "n", "type": "integer", "description": "qtd", "required": False, "default_value": 3},
        ],
        "headers": {"X-Cliente": "valor-de-teste", "X-Outro": "outro-valor"},
    }
    async with admin_world(monkeypatch) as world:
        created = await world.client.post(f"{BASE}/tools", json=body, headers=ADMIN)
        first = await world.client.get(f"{BASE}/tools/busca", headers=ADMIN)
        replaced = await world.client.put(f"{BASE}/tools/busca", json=first.json(), headers=if_match(1))
        second = await world.client.get(f"{BASE}/tools/busca", headers=ADMIN)

    assert created.status_code == 201 and replaced.status_code == 200
    assert second.json() == {**first.json(), "version": 2}
    assert first.json()["headers"] == {"X-Cliente": "***", "X-Outro": "***"}
    assert first.json()["http_method"] == "POST"
    stored = (await world.tools.list_all())[0].entity
    assert stored.headers == body["headers"] and [p.name for p in stored.parameters] == ["q", "n"]


async def test_ciclo_get_put_de_agente_com_rag_e_team_com_todos_os_campos(monkeypatch: pytest.MonkeyPatch):
    rag = {
        "active": True,
        "doc_name": "d",
        "model": "m",
        "factory_ia_model": "openai",
        "search_strategy": "hierarchical",
    }
    full_agent = agent_body(
        "a1",
        descricao="desc",
        active=False,
        user_memory_active=True,
        summary_active=True,
        rag_config=rag,
        model_params={"temperature": 0.2},
        base_url="https://llm.example.invalid",
        api_key_ref="env:OPENAI_API_KEY",
    )
    full_team = {
        "id": "t1",
        "nome": "T",
        "factory_ia_model": "ollama",
        "model": "q",
        "member_ids": ["a1"],
        "mode": "coordinate",
        "descricao": "d",
        "prompt": "p",
        "user_memory_active": False,
        "summary_active": True,
        "active": False,
        "model_params": {"temperature": 0.1},
    }
    async with admin_world(monkeypatch) as world:
        await world.client.post(f"{BASE}/agents", json=full_agent, headers=ADMIN)
        await world.client.post(f"{BASE}/teams", json=full_team, headers=ADMIN)
        results = {}
        for kind, entity_id in (("agents", "a1"), ("teams", "t1")):
            before = (await world.client.get(f"{BASE}/{kind}/{entity_id}", headers=ADMIN)).json()
            put = await world.client.put(f"{BASE}/{kind}/{entity_id}", json=before, headers=if_match(1))
            after = (await world.client.get(f"{BASE}/{kind}/{entity_id}", headers=ADMIN)).json()
            results[kind] = (before, put.status_code, after)

    for before, status, after in results.values():
        assert status == 200 and after == {**before, "version": 2}
    assert results["agents"][0]["rag_config"]["search_strategy"] == "hierarchical"
    assert results["teams"][0]["mode"] == "coordinate" and results["teams"][0]["active"] is False


# ── integridade referencial com inativos ────────────────────────────────────────────────────


async def test_inativos_contam_para_existir_e_para_estar_em_uso(monkeypatch: pytest.MonkeyPatch):
    inactive_tool = tool("velha")
    inactive_tool = Tool(**{**inactive_tool.__dict__, "active": False})
    inactive_team = TeamConfig(**{**team("t0", ["a1"]).__dict__, "active": False})
    async with admin_world(
        monkeypatch, agents=[agent("a1"), agent("a3", active=False)], teams=[inactive_team], tools=[inactive_tool]
    ) as world:
        uses_inactive_tool = await world.client.post(
            f"{BASE}/agents", json=agent_body("a2", tools_ids=["velha"]), headers=ADMIN
        )
        # Inativo: um team ativo só com membro inativo não monta no runtime e seria recusado (FA-04).
        uses_inactive_member = await world.client.post(
            f"{BASE}/teams",
            json={
                "id": "t2", "nome": "T", "factory_ia_model": "o", "model": "m", "member_ids": ["a3"], "active": False
            },
            headers=ADMIN,
        )
        soft_by_inactive_team = await world.client.delete(f"{BASE}/agents/a1", headers=if_match(0))
        hard_tool_in_use = await world.client.delete(
            f"{BASE}/tools/velha", params={"hard": "true"}, headers=if_match(0)
        )

    assert uses_inactive_tool.status_code == 201 and uses_inactive_member.status_code == 201
    assert soft_by_inactive_team.status_code == 409
    assert soft_by_inactive_team.json()["used_by"] == {"teams": ["t0"]}
    assert hard_tool_in_use.json()["used_by"] == {"agents": ["a2"]}


async def test_put_que_troca_referencias_confere_as_novas_e_libera_as_antigas(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(
        monkeypatch, agents=[agent("a1", tools_ids=["cep"])], tools=[tool("cep"), tool("outra")]
    ) as world:
        bad = await world.client.put(
            f"{BASE}/agents/a1", json=agent_body("a1", tools_ids=["cep", "sumiu"]), headers=if_match(0)
        )
        swap = await world.client.put(
            f"{BASE}/agents/a1", json=agent_body("a1", tools_ids=["outra"]), headers=if_match(0)
        )
        free = await world.client.delete(f"{BASE}/tools/cep", params={"hard": "true"}, headers=if_match(0))
        held = await world.client.delete(f"{BASE}/tools/outra", headers=if_match(0))
        bad_team = await world.client.put(f"{BASE}/teams/nada", json={"member_ids": ["a1"]}, headers=if_match(0))

    assert (bad.status_code, bad.json()["indexes"]) == (422, [1])
    assert (swap.status_code, free.status_code, held.status_code) == (200, 204, 409)
    assert bad_team.status_code == 404


async def test_used_by_sem_repeticao_com_id_repetido_de_quem_usa(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(
        monkeypatch, agents=[agent("a1", tools_ids=["cep", "cep"]), agent("a1", tools_ids=["cep"])], tools=[tool("cep")]
    ) as world:
        response = await world.client.delete(f"{BASE}/tools/cep", headers=if_match(0))

    assert response.status_code == 409 and response.json()["used_by"] == {"agents": ["a1"]}


# ── hard delete de tool sem uso e soft que preserva o segredo ───────────────────────────────


async def test_hard_delete_de_tool_sem_uso_e_soft_delete_preserva_o_header_gravado(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(
        monkeypatch, tools=[tool("a", headers={"X-K": "segredo-a"}), tool("b", headers={"X-K": "segredo-b"})]
    ) as world:
        soft = await world.client.delete(f"{BASE}/tools/a", headers=if_match(0))
        hard = await world.client.delete(f"{BASE}/tools/b", params={"hard": "true"}, headers=if_match(0))
        gone = await world.client.get(f"{BASE}/tools/b", headers=ADMIN)

    assert (soft.status_code, hard.status_code, gone.status_code) == (200, 204, 404)
    assert hard.content == b""
    stored = await world.tools.list_all()
    assert [(i.entity.id, i.entity.active, i.entity.headers) for i in stored] == [("a", False, {"X-K": "segredo-a"})]


# ── máscara *** e a origem da route ─────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    ("route", "status", "field"),
    [
        ("https://api.example.invalid/outro/caminho", 200, None),
        ("https://API.Example.INVALID/cep/{cep}", 200, None),
        ("http://api.example.invalid/cep/{cep}", 422, "headers"),
        ("https://api.example.invalid:8443/cep/{cep}", 422, "headers"),
        ("https://api.example.invalid:443/cep/{cep}", 422, "headers"),
        ("https://api.example.invalid.evil.example/cep", 422, "headers"),
        ("https://coletor.example.invalid/cep", 422, "headers"),
        # FA-06: a guarda de destino recusa a forma da route antes da máscara
        ("https://api.example.invalid:99999999/cep", 422, "route"),
        ("//api.example.invalid/cep", 422, "route"),
        ("/cep/{cep}", 422, "route"),
    ],
)
async def test_header_mascarado_so_acompanha_a_mesma_origem_da_route(
    monkeypatch: pytest.MonkeyPatch, route: str, status: int, field: str | None
):
    secret = "Bearer valor-de-teste-12345"  # noqa: S105  # valor de teste
    async with admin_world(monkeypatch, tools=[tool("cep", headers={"Authorization": secret})]) as world:
        shown = (await world.client.get(f"{BASE}/tools/cep", headers=ADMIN)).json()
        response = await world.client.put(f"{BASE}/tools/cep", json={**shown, "route": route}, headers=if_match(0))

    assert response.status_code == status
    assert secret not in response.text
    stored = (await world.tools.list_all())[0].entity
    if status == 422:
        assert response.json() == {"detail": "Configuração inválida", "field": field}
        assert stored.route == "https://api.example.invalid/cep/{cep}"
    assert stored.headers == {"Authorization": secret}


async def test_mascara_misturada_com_header_novo_e_header_removido(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(
        monkeypatch, tools=[tool("cep", headers={"Authorization": "segredo-1", "X-Velho": "segredo-2"})]
    ) as world:
        shown = (await world.client.get(f"{BASE}/tools/cep", headers=ADMIN)).json()
        mixed = await world.client.put(
            f"{BASE}/tools/cep",
            json={**shown, "headers": {"Authorization": "***", "X-Novo": "valor-novo"}},
            headers=if_match(0),
        )
        wrong_case = await world.client.put(
            f"{BASE}/tools/cep", json={**shown, "headers": {"authorization": "***"}}, headers=if_match(1)
        )
        literal_on_create = await world.client.post(
            f"{BASE}/tools",
            json={
                "id": "nova",
                "name": "n",
                "description": "d",
                "route": "https://x.example.invalid/",
                "headers": {"A": "***"},
            },
            headers=ADMIN,
        )
        dropped = await world.client.put(
            f"{BASE}/tools/cep", json={k: v for k, v in shown.items() if k != "headers"}, headers=if_match(1)
        )

    assert mixed.status_code == 200 and mixed.json()["headers"] == {"Authorization": "***", "X-Novo": "***"}
    assert wrong_case.status_code == 422 and wrong_case.json()["field"] == "headers"
    assert literal_on_create.status_code == 422 and literal_on_create.json()["field"] == "headers"
    assert dropped.status_code == 200 and not dropped.json().get("headers")
    assert (await world.tools.list_all())[0].entity.headers in (None, {})


# ── concorrência sob a trava (repositório que cede o loop a cada operação) ──────────────────


class _YieldingAgents(InMemoryAgentConfigRepository):
    async def list_all(self):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0)
        return await super().list_all()

    async def save(self, entity: AgentConfig, expected_version: int) -> int:
        await asyncio.sleep(0)
        return await super().save(entity, expected_version)


class _YieldingTools(InMemoryToolRepository):
    async def list_all(self):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0)
        return await super().list_all()

    async def delete(self, entity_id: str, expected_version: int) -> None:
        await asyncio.sleep(0)
        await super().delete(entity_id, expected_version)


async def test_dois_put_simultaneos_na_mesma_versao_um_vence_e_o_outro_recebe_412(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch) as world:
        agents = _YieldingAgents([agent("a1")])
        world.app.state.config_admin = fake_config_admin(agents, world.teams, world.tools, world.logger)
        results = await asyncio.gather(
            *(
                world.client.put(f"{BASE}/agents/a1", json=agent_body("a1", prompt=f"p{n}"), headers=if_match(0))
                for n in range(8)
            )
        )

    assert sorted(r.status_code for r in results) == [200] + [412] * 7
    assert [i.version for i in await agents.list_all()] == [1]


async def test_posts_simultaneos_do_mesmo_id_um_cria_e_os_outros_recebem_409(monkeypatch: pytest.MonkeyPatch):
    async with admin_world(monkeypatch) as world:
        agents = _YieldingAgents()
        world.app.state.config_admin = fake_config_admin(agents, world.teams, world.tools, world.logger)
        results = await asyncio.gather(
            *(world.client.post(f"{BASE}/agents", json=agent_body("a1"), headers=ADMIN) for _ in range(6))
        )

    assert sorted(r.status_code for r in results) == [201] + [409] * 5
    assert len(await agents.list_all()) == 1


async def test_criar_agente_com_tool_e_remover_a_tool_ao_mesmo_tempo_nunca_deixa_referencia_solta(
    monkeypatch: pytest.MonkeyPatch,
):
    for _ in range(5):
        async with admin_world(monkeypatch, tools=[tool("cep")]) as world:
            agents, tools = _YieldingAgents(), _YieldingTools([tool("cep")])
            world.app.state.config_admin = fake_config_admin(agents, world.teams, tools, world.logger)
            create, remove = await asyncio.gather(
                world.client.post(f"{BASE}/agents", json=agent_body("a1", tools_ids=["cep"]), headers=ADMIN),
                world.client.delete(f"{BASE}/tools/cep", params={"hard": "true"}, headers=if_match(0)),
            )
            stored_agents = await agents.list_all()
            stored_tools = {i.entity.id for i in await tools.list_all()}

        assert {create.status_code, remove.status_code} in ({201, 409}, {422, 204})
        for item in stored_agents:
            assert set(item.entity.tools_ids or []) <= stored_tools


class _YieldingTeams(InMemoryTeamConfigRepository):
    async def list_all(self):  # type: ignore[no-untyped-def]
        await asyncio.sleep(0)
        return await super().list_all()

    async def save(self, entity: TeamConfig, expected_version: int) -> int:
        await asyncio.sleep(0)
        return await super().save(entity, expected_version)


async def test_put_de_team_com_membro_e_hard_delete_do_membro_ao_mesmo_tempo_nunca_deixa_referencia_solta(
    monkeypatch: pytest.MonkeyPatch,
):
    """A trava cobre tipos diferentes: team referenciando agente que some na mesma hora."""
    for _ in range(5):
        async with admin_world(monkeypatch) as world:
            agents = _YieldingAgents([agent("a1"), agent("a2")])
            teams = _YieldingTeams([team("t1", ["a2"])])
            world.app.state.config_admin = fake_config_admin(agents, teams, world.tools, world.logger)
            put, remove = await asyncio.gather(
                world.client.put(
                    f"{BASE}/teams/t1",
                    json={
                        "id": "t1",
                        "nome": "T",
                        "factory_ia_model": "ollama",
                        "model": "qwen3",
                        "member_ids": ["a1"],
                    },
                    headers=if_match(0),
                ),
                world.client.delete(f"{BASE}/agents/a1", params={"hard": "true"}, headers=if_match(0)),
            )
            stored_teams = await teams.list_all()
            stored_agents = {i.entity.id for i in await agents.list_all()}

        assert {put.status_code, remove.status_code} in ({200, 409}, {422, 204})
        for item in stored_teams:
            assert set(item.entity.member_ids) <= stored_agents


# ── lifespan com o container real e store Mongo (coleções falsas) ───────────────────────────


@pytest.mark.usefixtures("offline_knowledge")
def test_pilha_completa_com_store_mongo_cria_altera_e_remove_pelo_lifespan(monkeypatch: pytest.MonkeyPatch):
    admin_env(monkeypatch)
    monkeypatch.setenv("AGNO_TELEMETRY", "false")
    with startup_world(Events(), agents=("a1",)):
        client = mongo_base.MongoClientFactory.get_client("qualquer")
        collections = client["qualquer"]
        assign_seed_ids()
        app = app_factory.AppFactory().create_app(routers=[admin_config_router])
        with loopback_client(app) as http:
            listed = http.get(f"{BASE}/agents", headers=ADMIN)
            tool_body = {
                "id": "cep",
                "name": "cep",
                "description": "d",
                "route": "https://api.example.invalid/cep",
                "headers": {"X-K": "segredo-de-teste"},
            }
            created_tool = http.post(f"{BASE}/tools", json=tool_body, headers=ADMIN)
            put_agent = http.put(f"{BASE}/agents/a1", json=agent_body("a1", tools_ids=["cep"]), headers=if_match(0))
            in_use = http.delete(f"{BASE}/tools/cep", params={"hard": "true"}, headers=if_match(1))
            run_key = http.get(f"{BASE}/agents", headers={"Authorization": f"Bearer {RUN_KEY}"})
            has_state = hasattr(app.state, "config_admin")
        stored_agent = collections["agents_config"].docs[0]

    assert has_state
    assert listed.status_code == 200 and [(i["id"], i["version"]) for i in listed.json()["items"]] == [("a1", 0)]
    assert (created_tool.status_code, put_agent.status_code, in_use.status_code) == (201, 200, 409)
    assert in_use.json()["used_by"] == {"agents": ["a1"]}
    assert run_key.status_code == 403
    assert (
        stored_agent["version"] == 1 and stored_agent["tools_ids"] == ["cep"] and "factoryIaModel" not in stored_agent
    )
    assert collections["tools"].docs[0]["headers"] == {"X-K": "segredo-de-teste"}
    assert json.dumps(listed.json()).find("segredo-de-teste") == -1
