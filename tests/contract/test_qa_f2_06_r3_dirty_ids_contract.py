"""QA do F2-06 (rodada 3): bordas da limpeza de listas de ids, iguais no Mongo e no YAML.

Complementa ``test_config_documents_contract.py`` (casos comuns de ``tools_ids``/``member_ids``): aqui
lista enorme, teto de posições, duas grafias do mesmo campo, id do documento não-texto e vazamento de
valor hostil em qualquer registro de log.
"""

from __future__ import annotations

import time
from typing import Any

import pytest

from tests.fakes.config_stores import (
    MARKER,
    NOT_A_LIST,
    NOT_TEXT_ITEMS,
    logged_errors,
    marked_agent_doc,
    marked_team_doc,
)
from tests.fakes.logger import RecordingLogger

BIG = 50_000
# YAML puro-Python lê 50 mil itens em ~1 s; o teto é folgado de propósito, só pega comportamento quadrático.
LOAD_BUDGET_SECONDS = 20.0


async def test_lista_enorme_de_itens_sujos_gera_um_registro_e_carrega_rapido(backend):
    logger = RecordingLogger()
    repo = backend.agents([marked_agent_doc("enorme", tools_ids=["a", *range(BIG), "b"])], logger)

    started = time.perf_counter()
    configs = await repo.get_active_agents()
    elapsed = time.perf_counter() - started

    assert [(c.id, c.tools_ids) for c in configs] == [("enorme", ["a", "b"])]
    assert len(logger.records) == 1
    assert logged_errors(logger) == [
        (
            NOT_TEXT_ITEMS,
            {"agent_id": "enorme", "field": "tools_ids", "dropped_count": BIG, "item_positions": list(range(2, 12))},
        )
    ]
    assert elapsed < LOAD_BUDGET_SECONDS


async def test_lista_enorme_de_membros_sujos_gera_um_registro_e_o_team_sobe(backend):
    logger = RecordingLogger()
    repo = backend.teams([marked_team_doc("enorme", member_ids=[None] * BIG + ["ok"])], logger)

    assert [(c.id, c.member_ids) for c in await repo.get_active_teams()] == [("enorme", ["ok"])]
    assert logged_errors(logger) == [
        (
            NOT_TEXT_ITEMS,
            {"team_id": "enorme", "field": "member_ids", "dropped_count": BIG, "item_positions": list(range(1, 11))},
        )
    ]


@pytest.mark.parametrize(
    ("dirty", "positions"), [(9, list(range(1, 10))), (10, list(range(1, 11))), (11, list(range(1, 11)))]
)
async def test_teto_de_dez_posicoes_no_log_e_contagem_inteira(backend, dirty, positions):
    logger = RecordingLogger()
    repo = backend.agents([marked_agent_doc("teto", tools_ids=[*([0] * dirty), "t"])], logger)

    assert [c.tools_ids for c in await repo.get_active_agents()] == [["t"]]
    ((_, context),) = logged_errors(logger)
    assert (context["dropped_count"], context["item_positions"]) == (dirty, positions)


async def test_lista_so_de_texto_nao_gera_log_nem_muda(backend):
    logger = RecordingLogger()
    repo = backend.agents([marked_agent_doc("limpo", tools_ids=["a", "b", "a"])], logger)
    teams = backend.teams([marked_team_doc("limpo", member_ids=["a", "b"])], logger)

    assert [c.tools_ids for c in await repo.get_active_agents()] == [["a", "b", "a"]]
    assert [c.member_ids for c in await teams.get_active_teams()] == [["a", "b"]]
    assert logger.records == []


async def test_com_as_duas_grafias_de_member_ids_vale_a_snake_case_e_so_ela_e_conferida(backend):
    """``member_ids`` limpa + ``memberIds`` suja: nada é logado; ``member_ids`` suja: o log cita ``member_ids``."""
    logger = RecordingLogger()
    clean_snake = marked_team_doc("a", member_ids=["x"]) | {"memberIds": [None, {"k": MARKER}]}
    dirty_snake = marked_team_doc("b", member_ids=["y", 1]) | {"memberIds": ["z"]}
    repo = backend.teams([clean_snake, dirty_snake], logger)

    assert [(c.id, c.member_ids) for c in await repo.get_active_teams()] == [("a", ["x"]), ("b", ["y"])]
    assert logged_errors(logger) == [
        (NOT_TEXT_ITEMS, {"team_id": "b", "field": "member_ids", "dropped_count": 1, "item_positions": [2]})
    ]
    assert all(MARKER not in repr(r) for r in logger.records)


async def test_member_ids_nulo_explicito_continua_invalidando_o_team_com_log_do_tipo(backend):
    logger = RecordingLogger()
    # a chave presente (mesmo nula) vence a legada
    doc = marked_team_doc("nulo", member_ids=None) | {"memberIds": ["z"]}
    repo = backend.teams([doc, marked_team_doc("bom")], logger)

    assert [c.id for c in await repo.get_active_teams()] == ["bom"]
    errors = logged_errors(logger)
    assert errors[0] == (NOT_A_LIST, {"team_id": "nulo", "field": "member_ids", "value_type": "NoneType"})
    assert [(m, c["team_id"]) for m, c in errors[1:]] == [("Documento de team inválido ignorado", "nulo")]


@pytest.mark.parametrize("bad_id", [[MARKER], {"$ne": MARKER}, 5, None], ids=["lista", "mapa", "numero", "nulo"])
async def test_id_nao_texto_com_lista_suja_loga_id_nulo_e_nunca_o_valor(backend, bad_id):
    logger = RecordingLogger()
    repo = backend.agents([marked_agent_doc(bad_id, tools_ids={"a": MARKER}), marked_agent_doc("bom")], logger)

    assert [c.id for c in await repo.get_active_agents()] == ["bom"]
    errors = logged_errors(logger)
    assert errors[0] == (NOT_A_LIST, {"agent_id": None, "field": "tools_ids", "value_type": "dict"})
    assert all(MARKER not in repr(r) for r in logger.records)


async def test_nenhum_valor_hostil_aparece_em_nenhum_registro_de_log(backend):
    """Todo tipo de item sujo, em agentes e teams, em listagem e em busca por id: o marcador nunca vai ao log."""
    hostile: list[Any] = [
        MARKER, [MARKER], {MARKER: MARKER}, {"k": MARKER}, (MARKER,), 1.5, True, None, b"x", [[MARKER]],
    ]
    logger = RecordingLogger()
    agents = backend.agents(
        [
            marked_agent_doc("lista", tools_ids=["t", *[x for x in hostile if not isinstance(x, str)]]),
            marked_agent_doc("texto", tools_ids=MARKER),
            marked_agent_doc("mapa", tools_ids={MARKER: [MARKER]}),
        ],
        logger,
    )
    non_text = [x for x in hostile if not isinstance(x, str)]
    docs = [marked_team_doc("t1", member_ids=["m", *non_text]), marked_team_doc("t2", member_ids=MARKER)]
    teams = backend.teams(docs, logger)

    await agents.get_active_agents()
    await agents.get_agent_by_id("lista")
    await teams.get_active_teams()
    await teams.get_team_by_id("t1")

    assert any(r.message == NOT_TEXT_ITEMS for r in logger.records)
    assert all(MARKER not in repr(r) for r in logger.records)
    for record in logger.records:
        if record.message in (NOT_TEXT_ITEMS, NOT_A_LIST):
            assert set(record.context) <= {
                "agent_id", "team_id", "field", "value_type", "dropped_count", "item_positions"
            }
