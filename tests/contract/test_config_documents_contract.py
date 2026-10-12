"""Contrato dos backends de config que leem **documentos** (``CONFIG_STORE=mongo|yaml``).

Mesmo documento, mesmo resultado nos dois: mesmo mapper e mesma validação de domínio
(``src/infrastructure/repositories/config_documents.py``). Documento inválido isola só ele (F1-10):
vira log de erro com o id (se for texto), a localização no backend (``mongo_id`` ou ``position``) e o
tipo do erro, nunca o conteúdo; os válidos carregam. Só ``active: true`` (booleano) entra nas listagens.
"""

from __future__ import annotations

import pytest

from tests.fakes.config_stores import (
    MARKER,
    NOT_A_LIST,
    NOT_TEXT_ITEMS,
    MongoBackend,
    YamlBackend,
    logged_errors,
    marked_agent_doc,
    marked_team_doc,
    marked_tool_doc,
)
from tests.fakes.logger import RecordingLogger

LOCATION_KEY = {"mongo": "mongo_id", "yaml": "position"}


def _assert_invalid_logged(
    logger: RecordingLogger, backend: MongoBackend | YamlBackend, message: str, id_field: str, ids: list[str | None]
) -> None:
    errors = logged_errors(logger)
    assert [m for m, _ in errors] == [message] * len(ids)
    assert [ctx[id_field] for _, ctx in errors] == ids
    for _, ctx in errors:
        assert set(ctx) == {id_field, LOCATION_KEY[backend.name], "error_type"}
        assert ctx["error_type"] in ("ValueError", "KeyError")
    assert all(MARKER not in repr(r) for r in logger.records)


async def test_agente_invalido_isolado_e_os_validos_carregam(backend):
    logger = RecordingLogger()
    docs = [
        marked_agent_doc("antes"),
        {k: v for k, v in marked_agent_doc().items() if k != "id"},
        marked_agent_doc(123),
        marked_agent_doc("sem-nome", nome=""),
        marked_agent_doc("rag-estrategia", rag_config={"active": True, "search_strategy": "vetorial"}),
        marked_agent_doc("depois"),
    ]
    repo = backend.agents(docs, logger)

    assert [c.id for c in await repo.get_active_agents()] == ["antes", "depois"]
    _assert_invalid_logged(
        logger, backend, "Documento de agente inválido ignorado", "agent_id", [None, None, "sem-nome", "rag-estrategia"]
    )


async def test_team_invalido_isolado_e_os_validos_carregam(backend):
    logger = RecordingLogger()
    docs = [
        marked_team_doc("antes"),
        marked_team_doc(["lista"]),
        marked_team_doc("modo", mode="aleatorio"),
        marked_team_doc("sem-membros", member_ids=[]),
        marked_team_doc("depois"),
    ]
    repo = backend.teams(docs, logger)

    assert [c.id for c in await repo.get_active_teams()] == ["antes", "depois"]
    _assert_invalid_logged(
        logger, backend, "Documento de team inválido ignorado", "team_id", [None, "modo", "sem-membros"]
    )


@pytest.mark.parametrize("method", ["by_ids", "all_active"])
async def test_tool_invalida_isolada_e_as_validas_carregam(backend, method):
    logger = RecordingLogger()
    docs = [
        marked_tool_doc("antes"),
        marked_tool_doc("sem-rota", route=""),
        marked_tool_doc("metodo", http_method="TRACE"),
        marked_tool_doc("depois"),
    ]
    repo = backend.tools(docs, logger)

    if method == "by_ids":
        tools = await repo.get_tools_by_ids(["antes", "sem-rota", "metodo", "depois"])
    else:
        tools = await repo.get_all_active_tools()

    assert [t.id for t in tools] == ["antes", "depois"]
    _assert_invalid_logged(logger, backend, "Documento de tool inválido ignorado", "tool_id", ["sem-rota", "metodo"])


async def test_tool_com_id_repetido_fica_com_a_primeira_e_loga_a_repetida(backend):
    logger = RecordingLogger()
    repo = backend.tools([marked_tool_doc("dup", name="primeira"), marked_tool_doc("dup", name="segunda")], logger)

    assert [t.name for t in await repo.get_tools_by_ids(["dup"])] == ["primeira"]
    assert logged_errors(logger) == [("Tool com id repetido ignorada", {"tool_id": "dup"})]


@pytest.mark.parametrize("active", [False, "true", 1, None], ids=["false", "texto", "numero", "nulo"])
async def test_so_active_true_booleano_entra_nas_listagens(backend, active):
    """Como a consulta ``{"active": True}`` do Mongo (BSON: ``1`` e ``"true"`` não casam com ``true``)."""
    agents = backend.agents([marked_agent_doc("sim"), marked_agent_doc("nao", active=active)])
    teams = backend.teams([marked_team_doc("sim"), marked_team_doc("nao", active=active)])
    tools = backend.tools([marked_tool_doc("sim"), marked_tool_doc("nao", active=active)])

    assert [c.id for c in await agents.get_active_agents()] == ["sim"]
    assert [c.id for c in await teams.get_active_teams()] == ["sim"]
    assert [t.id for t in await tools.get_all_active_tools()] == ["sim"]
    assert [t.id for t in await tools.get_tools_by_ids(["sim", "nao"])] == ["sim"]


async def test_documento_sem_active_nao_entra_na_listagem_mas_e_achado_por_id(backend):
    doc = {k: v for k, v in marked_agent_doc("sem-active").items() if k != "active"}
    repo = backend.agents([doc])

    assert await repo.get_active_agents() == []
    assert (await repo.get_agent_by_id("sem-active")).active is True


async def test_formato_legado_camel_case_vira_a_mesma_entidade(backend):
    legacy_team = {k: v for k, v in marked_team_doc("legado").items() if k != "member_ids"} | {
        "memberIds": ["a", "b"],
        "userMemoryActive": False,
        "summaryActive": True,
    }
    teams = backend.teams([legacy_team])
    agents = backend.agents([marked_agent_doc("legado", factoryIaModel="openai", prompt=["l1", "l2"])])

    (team,) = await teams.get_active_teams()
    (agent,) = await agents.get_active_agents()

    assert (team.member_ids, team.user_memory_active, team.summary_active) == (["a", "b"], False, True)
    assert (agent.factory_ia_model, agent.prompt) == ("openai", ["l1", "l2"])


async def test_campos_novos_em_camel_case_sao_ignorados_com_aviso_sem_valores(backend):
    logger = RecordingLogger()
    repo = backend.agents([marked_agent_doc("camel", apiKeyRef=f"env:{MARKER}", baseUrl=MARKER)], logger)

    (config,) = await repo.get_active_agents()

    assert (config.api_key_ref, config.base_url) == (None, None)
    warnings = [(r.message, r.context) for r in logger.records if r.level == "warning"]
    assert [(m, ctx["agent_id"], ctx["keys"]) for m, ctx in warnings] == [
        (
            "Documento com chaves camelCase ignoradas; use model_params, base_url e api_key_ref",
            "camel",
            ["apiKeyRef", "baseUrl"],
        )
    ]
    assert all(MARKER not in repr(r) for r in logger.records)


async def test_busca_por_id_de_documento_invalido_levanta_erro_de_validacao(backend):
    """Na busca por id não há lista para isolar: o erro de validação sobe (sem o conteúdo)."""
    repo = backend.agents([marked_agent_doc("ruim", nome="")])

    with pytest.raises(ValueError, match="Nome do agente") as exc_info:
        await repo.get_agent_by_id("ruim")

    assert MARKER not in str(exc_info.value)


# ── tipos aceitos em silêncio antes (QA do F2-06): listas de id e campos HTTP da tool ──




@pytest.mark.parametrize(
    ("tools_ids", "value_type"),
    [({"a": MARKER}, "dict"), (MARKER, "str"), (7, "int")],
    ids=["mapa", "texto", "numero"],
)
async def test_tools_ids_que_nao_e_lista_sobe_o_agente_sem_tools_com_log(backend, tools_ids, value_type):
    """Documento que já existia com ``tools_ids`` sujo continua carregando (degradado), nos dois backends."""
    logger = RecordingLogger()
    repo = backend.agents([marked_agent_doc("sujo", tools_ids=tools_ids), marked_agent_doc("bom")], logger)

    assert [(c.id, c.tools_ids) for c in await repo.get_active_agents()] == [("sujo", []), ("bom", [])]
    assert logged_errors(logger) == [(NOT_A_LIST, {"agent_id": "sujo", "field": "tools_ids", "value_type": value_type})]
    assert all(MARKER not in repr(r) for r in logger.records)


@pytest.mark.parametrize(
    ("tools_ids", "kept", "dropped"),
    [
        (["a", 1, "b"], ["a", "b"], [2]),
        (["a", None], ["a"], [2]),
        ([[MARKER], "a", {"x": MARKER}], ["a"], [1, 3]),
        ([True], [], [1]),
    ],
    ids=["numero", "nulo", "lista-e-mapa", "so-item-invalido"],
)
async def test_itens_de_tools_ids_que_nao_sao_texto_sao_descartados_com_um_log(backend, tools_ids, kept, dropped):
    logger = RecordingLogger()
    repo = backend.agents([marked_agent_doc("sujo", tools_ids=tools_ids)], logger)

    assert [(c.id, c.tools_ids) for c in await repo.get_active_agents()] == [("sujo", kept)]
    assert logged_errors(logger) == [
        (
            NOT_TEXT_ITEMS,
            {"agent_id": "sujo", "field": "tools_ids", "dropped_count": len(dropped), "item_positions": dropped},
        )
    ]
    assert all(MARKER not in repr(r) for r in logger.records)


async def test_lista_de_ids_com_muitos_itens_sujos_gera_um_log_so_com_as_primeiras_posicoes(backend):
    """Um log por campo por documento, com teto de posições: lista enorme não vira enxurrada de log."""
    logger = RecordingLogger()
    tools_ids = ["a", *range(500), "b"]
    repo = backend.agents([marked_agent_doc("sujo", tools_ids=tools_ids)], logger)

    assert [(c.id, c.tools_ids) for c in await repo.get_active_agents()] == [("sujo", ["a", "b"])]
    assert logged_errors(logger) == [
        (
            NOT_TEXT_ITEMS,
            {"agent_id": "sujo", "field": "tools_ids", "dropped_count": 500, "item_positions": list(range(2, 12))},
        )
    ]


async def test_busca_por_id_de_agente_com_tools_ids_sujo_devolve_o_agente_degradado(backend):
    logger = RecordingLogger()
    repo = backend.agents([marked_agent_doc("sujo", tools_ids=["a", 1])], logger)

    assert (await repo.get_agent_by_id("sujo")).tools_ids == ["a"]
    assert logged_errors(logger) == [
        (NOT_TEXT_ITEMS, {"agent_id": "sujo", "field": "tools_ids", "dropped_count": 1, "item_positions": [2]})
    ]


async def test_tools_ids_nulo_ou_ausente_continua_valido(backend):
    logger = RecordingLogger()
    without = {k: v for k, v in marked_agent_doc("ausente").items() if k != "tools_ids"}
    repo = backend.agents([marked_agent_doc("nulo", tools_ids=None), without], logger)

    assert [(c.id, c.tools_ids) for c in await repo.get_active_agents()] == [("nulo", None), ("ausente", [])]
    assert logger.records == []


async def test_itens_de_member_ids_que_nao_sao_texto_sao_descartados_e_o_team_sobe_com_os_demais(backend):
    """O log cita a chave efetivamente lida (``member_ids`` ou o legado ``memberIds``)."""
    logger = RecordingLogger()
    legacy = {k: v for k, v in marked_team_doc("legado").items() if k != "member_ids"}
    legacy |= {"memberIds": [{"a": MARKER}, "b"]}
    repo = backend.teams([marked_team_doc("sujo", member_ids=["a", 3, "b", None]), legacy], logger)

    assert [(c.id, c.member_ids) for c in await repo.get_active_teams()] == [("sujo", ["a", "b"]), ("legado", ["b"])]
    assert logged_errors(logger) == [
        (NOT_TEXT_ITEMS, {"team_id": "sujo", "field": "member_ids", "dropped_count": 2, "item_positions": [2, 4]}),
        (NOT_TEXT_ITEMS, {"team_id": "legado", "field": "memberIds", "dropped_count": 1, "item_positions": [1]}),
    ]
    assert all(MARKER not in repr(r) for r in logger.records)


@pytest.mark.parametrize(
    ("member_ids", "cleanup_log"),
    [
        (MARKER, (NOT_A_LIST, {"field": "member_ids", "value_type": "str"})),
        ({"a": MARKER}, (NOT_A_LIST, {"field": "member_ids", "value_type": "dict"})),
        ([None], (NOT_TEXT_ITEMS, {"field": "member_ids", "dropped_count": 1, "item_positions": [1]})),
    ],
    ids=["texto", "mapa", "lista-so-com-nulo"],
)
async def test_member_ids_sem_nenhum_id_de_texto_continua_invalidando_o_team(backend, member_ids, cleanup_log):
    """A limpeza não muda a regra do team: sem membro válido, documento inválido (isolado)."""
    logger = RecordingLogger()
    repo = backend.teams([marked_team_doc("ruim", member_ids=member_ids), marked_team_doc("bom")], logger)

    assert [c.id for c in await repo.get_active_teams()] == ["bom"]
    message, context = cleanup_log
    errors = logged_errors(logger)
    assert errors[0] == (message, {"team_id": "ruim", **context})
    assert [(m, ctx["team_id"], ctx["error_type"]) for m, ctx in errors[1:]] == [
        ("Documento de team inválido ignorado", "ruim", "ValueError")
    ]
    assert all(MARKER not in repr(r) for r in logger.records)


@pytest.mark.parametrize(
    "overrides",
    [
        {"route": 7},
        {"route": [MARKER]},
        {"headers": {"Accept": 1}},
        {"headers": {"X-Token": [MARKER]}},
        {"headers": {"X": None}},
        {"headers": [MARKER]},
        {"headers": MARKER},
    ],
    ids=[
        "route-numero", "route-lista", "header-numero", "header-lista", "header-nulo", "headers-lista", "headers-texto"
    ],
)
async def test_route_ou_headers_fora_do_tipo_invalida_a_tool(backend, overrides):
    logger = RecordingLogger()
    docs = [marked_tool_doc("ruim", **overrides), marked_tool_doc("boa", headers={"Accept": "application/json"})]
    repo = backend.tools(docs, logger)

    tools = await repo.get_all_active_tools()

    assert [(t.id, t.headers) for t in tools] == [("boa", {"Accept": "application/json"})]
    _assert_invalid_logged(logger, backend, "Documento de tool inválido ignorado", "tool_id", ["ruim"])
