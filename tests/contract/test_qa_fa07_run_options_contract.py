"""QA do FA-07: os campos de execução (``markdown``, ``num_history_runs``...) lidos igual pelos dois stores.

Mesmo documento, mesmo resultado em Mongo (coleção fake) e YAML: valores válidos chegam à entidade, ausentes
viram o default de antes, e valor de tipo/faixa errada isola só aquele documento (log sem o valor), sem
derrubar os vizinhos. O valor do documento é um marcador: se aparecer no log, vazou.
"""

from __future__ import annotations

from typing import Any

import pytest

from tests.fakes.config_stores import MARKER, logged_errors, marked_agent_doc, marked_team_doc
from tests.fakes.logger import RecordingLogger

AGENT_OPTIONS: dict[str, Any] = {
    "markdown": False,
    "add_history_to_context": False,
    "num_history_runs": 50,
    "add_datetime_to_context": False,
    "reasoning": True,
}
TEAM_OPTIONS: dict[str, Any] = {"markdown": False, "add_history_to_context": False}


async def test_campos_validos_chegam_a_entidade_e_ausentes_viram_o_default_de_antes(backend):
    agent_docs = [
        marked_agent_doc("com", **AGENT_OPTIONS),
        marked_agent_doc("sem"),
        marked_agent_doc("minimo", num_history_runs=1),
    ]
    agents = await backend.agents(agent_docs).get_active_agents()
    teams = await backend.teams([marked_team_doc("com", **TEAM_OPTIONS), marked_team_doc("sem")]).get_active_teams()

    by_id = {a.id: a for a in agents}
    run_options = ("markdown", "add_history_to_context", "num_history_runs", "add_datetime_to_context", "reasoning")
    assert {k: getattr(by_id["com"], k) for k in run_options} == AGENT_OPTIONS
    assert {k: getattr(by_id["sem"], k) for k in run_options} == {
        "markdown": True,
        "add_history_to_context": True,
        "num_history_runs": 5,
        "add_datetime_to_context": True,
        "reasoning": False,
    }
    assert by_id["minimo"].num_history_runs == 1
    team_by_id = {t.id: t for t in teams}
    assert (team_by_id["com"].markdown, team_by_id["com"].add_history_to_context) == (False, False)
    assert (team_by_id["sem"].markdown, team_by_id["sem"].add_history_to_context) == (True, True)


@pytest.mark.parametrize(
    "bad",
    [
        {"markdown": MARKER},
        {"markdown": 1},
        {"markdown": None},
        {"reasoning": "true"},
        {"add_history_to_context": 0},
        {"add_datetime_to_context": [MARKER]},
        {"num_history_runs": 0},
        {"num_history_runs": 51},
        {"num_history_runs": -1},
        {"num_history_runs": True},
        {"num_history_runs": 5.0},
        {"num_history_runs": MARKER},
        {"num_history_runs": None},
    ],
    ids=repr,
)
async def test_agente_com_campo_de_execucao_invalido_e_isolado_sem_vazar_o_valor(backend, bad):
    logger = RecordingLogger()
    docs = [marked_agent_doc("antes"), marked_agent_doc("ruim", **bad), marked_agent_doc("depois")]
    repo = backend.agents(docs, logger)

    assert [c.id for c in await repo.get_active_agents()] == ["antes", "depois"]
    errors = logged_errors(logger)
    assert [(m, ctx["agent_id"], ctx["error_type"]) for m, ctx in errors] == [
        ("Documento de agente inválido ignorado", "ruim", "InvalidField")
    ]
    assert all(MARKER not in repr(r) for r in logger.records)


@pytest.mark.parametrize("bad", [{"markdown": MARKER}, {"add_history_to_context": 1}, {"markdown": None}], ids=repr)
async def test_team_com_campo_de_execucao_invalido_e_isolado_sem_vazar_o_valor(backend, bad):
    logger = RecordingLogger()
    repo = backend.teams([marked_team_doc("antes"), marked_team_doc("ruim", **bad), marked_team_doc("depois")], logger)

    assert [c.id for c in await repo.get_active_teams()] == ["antes", "depois"]
    errors = logged_errors(logger)
    assert [(ctx["team_id"], ctx["error_type"]) for _, ctx in errors] == [("ruim", "InvalidField")]
    assert all(MARKER not in repr(r) for r in logger.records)
