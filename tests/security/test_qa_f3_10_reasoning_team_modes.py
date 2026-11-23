"""QA F3-10: ``expose_reasoning`` por membro em todos os modos de team (``TEAM_MODES``).

O teste do dev (``test_agui_reasoning_visibility``) cobre só ``coordinate``. Aqui, pelo AG-UI real, com as
fábricas reais e modelo scriptado, cada modo de team com um membro privado (``expose_reasoning=false``) e um
com o default: o texto do raciocínio do privado não sai em nenhum byte do SSE, o do membro com o default
chega (conferido em ``coordinate`` e ``broadcast``, onde o líder delega a ele), e a resposta do privado e a
síntese do líder chegam. Também: team aninhado respeita o membro privado.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from agno.models.response import ModelResponse
from agno.team import Team

from src.domain.entities.team_config import TEAM_MODES, TeamConfig
from src.infrastructure.runtime.agno.team_factory_service import TeamFactoryService
from tests.fakes import RecordingLogger
from tests.fakes.agui import text_of
from tests.fakes.session_db import fake_session_db
from tests.security.test_agui_reasoning_visibility import (
    ANALYSIS,
    PUBLIC_THOUGHT,
    THOUGHT,
    _agents,
    _agui,
    _app,
    _calls,
    _config,
    _reasoning_script,
    _ScriptedFactory,
    _think,
    _tool_call_ids,
    _tool_names,
)

pytestmark = pytest.mark.security

_TASK_ID = re.compile(r"Task created: \[([^\]]+)\]")
_MODES = ("route", "coordinate", "broadcast", "tasks")


def _execute_task(seen: str) -> ModelResponse:
    task_id = _TASK_ID.search(seen)
    assert task_id, seen
    return _calls(("t-2", "execute_task", {"task_id": task_id.group(1), "member_id": "privado"}))


def _leader_script(mode: str) -> list[Any]:
    if mode == "route":
        return [
            _calls(("t-1", "delegate_task_to_member", {"member_id": "privado", "task": "pense"})),
            "síntese do time",
        ]
    if mode == "broadcast":
        return [_calls(("t-1", "delegate_task_to_members", {"task": "pense"})), "síntese do time"]
    if mode == "coordinate":
        return [
            _calls(("t-1", "delegate_task_to_member", {"member_id": "privado", "task": "pense"})),
            _calls(("t-2", "delegate_task_to_member", {"member_id": "publico", "task": "pense"})),
            "síntese do time",
        ]
    assert mode == "tasks"
    return [
        _calls(("t-1", "create_task", {"title": "pensar", "description": "pense", "assignee": "privado"})),
        _execute_task,
        _calls(("t-3", "mark_all_complete", {"summary": "feito"})),
        "síntese do time",
    ]


def _team(
    db: Any, models: _ScriptedFactory, mode: str, members: list[Any], member_ids: list[str], team_id: str = "time"
) -> Team:
    config = TeamConfig(
        id=team_id,
        nome=team_id,
        factory_ia_model="fake",
        model=f"m-{team_id}",
        member_ids=member_ids,
        mode=mode,
        user_memory_active=False,
    )
    return TeamFactoryService(session_db=db, logger=RecordingLogger(), model_factory=models).create_team(
        config, members
    )


def test_todos_os_modos_de_team_estao_parametrizados():
    # Novo modo em TEAM_MODES sem cobertura aqui deve quebrar este teste, não passar em silêncio.
    assert set(TEAM_MODES) == set(_MODES)


@pytest.mark.parametrize("mode", _MODES)
async def test_em_todo_modo_de_team_o_raciocinio_do_membro_privado_nao_sai(monkeypatch: pytest.MonkeyPatch, mode: str):
    db = fake_session_db()
    scripts: dict[str, list[Any]] = {
        "m-privado": _reasoning_script("p", THOUGHT, "resposta do privado"),
        "m-publico": [_calls(_think("u-think", PUBLIC_THOUGHT)), "resposta do publico"],
        "m-time": _leader_script(mode),
    }
    models = _ScriptedFactory(scripts)
    service = _agents(db, models)
    private = await service.create_agent(_config("privado", expose_reasoning=False))
    public = await service.create_agent(_config("publico"))
    team = _team(db, models, mode, [private, public], ["privado", "publico"])

    raw, events = await _agui(_app(monkeypatch, db, [private, public], [team]), "time", f"t-{mode}")

    for secret in (THOUGHT, ANALYSIS):
        assert secret not in raw, mode
    assert not {"p-think", "p-analyze"} & _tool_call_ids(events)  # nenhum TOOL_CALL_* do membro privado
    assert "analyze" not in _tool_names(events)
    assert "resposta do privado" in raw  # a resposta do membro privado (resultado da delegação) chega igual
    if mode != "route":  # no route o time devolve a resposta do membro, sem síntese do líder
        assert "síntese do time" in text_of(events)
    # o raciocínio rodou no membro privado até o fim (think, analyze, resposta)
    private_model = next(m for m in models.models if m.id == "m-privado")
    assert len(private_model.calls) == 3
    if mode in {"coordinate", "broadcast"}:
        assert PUBLIC_THOUGHT in raw and "u-think" in _tool_call_ids(events)
        assert _tool_names(events) == ["think"]  # só o think do membro com o default


async def test_team_aninhado_respeita_o_membro_privado_do_subteam(monkeypatch: pytest.MonkeyPatch):
    db = fake_session_db()
    models = _ScriptedFactory(
        {
            "m-privado": _reasoning_script("p", THOUGHT, "resposta do privado"),
            "m-sub": [
                _calls(("s-1", "delegate_task_to_member", {"member_id": "privado", "task": "pense"})),
                "síntese do sub",
            ],
            "m-time": [
                _calls(("t-1", "delegate_task_to_member", {"member_id": "sub", "task": "pense"})),
                "síntese do time",
            ],
        }
    )
    private = await _agents(db, models).create_agent(_config("privado", expose_reasoning=False))
    sub = _team(db, models, "coordinate", [private], ["privado"], team_id="sub")
    top_config = TeamConfig(
        id="time",
        nome="time",
        factory_ia_model="fake",
        model="m-time",
        member_ids=["sub"],
        mode="coordinate",
        user_memory_active=False,
    )
    top = TeamFactoryService(session_db=db, logger=RecordingLogger(), model_factory=models).create_team(
        top_config, [sub]
    )

    raw, events = await _agui(_app(monkeypatch, db, [private], [sub, top]), "time", "t-aninhado")

    for secret in (THOUGHT, ANALYSIS):
        assert secret not in raw
    assert "think" not in _tool_names(events) and "analyze" not in _tool_names(events)
    assert "síntese do time" in text_of(events)
