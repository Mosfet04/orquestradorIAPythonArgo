"""Testes para GetActiveTeamsUseCase."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

import pytest

from src.application.use_cases.get_active_teams_use_case import GetActiveTeamsUseCase
from src.domain.entities.team_config import TeamConfig


def _make_team_config(team_id: str = "team-1") -> TeamConfig:
    return TeamConfig(
        id=team_id,
        nome=f"Team {team_id}",
        factory_ia_model="ollama",
        model="llama3.2:latest",
        member_ids=["agent-a"],
        mode="route",
    )


@pytest.fixture
def team_factory_service():
    service = MagicMock()
    service.create_team = MagicMock(return_value=MagicMock())
    return service


@pytest.fixture
def use_case(team_factory_service, mock_team_config_repository, mock_logger):
    return GetActiveTeamsUseCase(
        team_factory_service=team_factory_service,
        team_config_repository=mock_team_config_repository,
        logger=mock_logger,
    )


class TestGetActiveTeamsUseCase:
    async def test_execute_returns_teams(
        self, use_case, mock_team_config_repository, team_factory_service
    ):
        configs = [_make_team_config("t1"), _make_team_config("t2")]
        mock_team_config_repository.get_active_teams = AsyncMock(return_value=configs)
        agents = [MagicMock(), MagicMock()]

        result = await use_case.execute(agents)

        assert len(result) == 2
        assert team_factory_service.create_team.call_count == 2

    async def test_execute_empty_configs(
        self, use_case, mock_team_config_repository
    ):
        mock_team_config_repository.get_active_teams = AsyncMock(return_value=[])

        result = await use_case.execute([MagicMock()])

        assert result == []

    async def test_execute_skips_failed_team(
        self, use_case, mock_team_config_repository, team_factory_service, mock_logger
    ):
        configs = [_make_team_config("t1"), _make_team_config("t2")]
        mock_team_config_repository.get_active_teams = AsyncMock(return_value=configs)
        team_factory_service.create_team = MagicMock(
            side_effect=[ValueError("boom"), MagicMock()]
        )

        result = await use_case.execute([MagicMock()])

        assert len(result) == 1
        mock_logger.error.assert_called_once()

    async def test_execute_all_fail_returns_empty(
        self, use_case, mock_team_config_repository, team_factory_service
    ):
        configs = [_make_team_config("t1")]
        mock_team_config_repository.get_active_teams = AsyncMock(return_value=configs)
        team_factory_service.create_team = MagicMock(
            side_effect=RuntimeError("fail")
        )

        result = await use_case.execute([MagicMock()])

        assert result == []


async def test_falha_ao_criar_um_team_vira_log_de_erro_com_id_e_tipo():
    from tests.fakes import InMemoryTeamConfigRepository, RecordingLogger

    logger = RecordingLogger()
    factory = MagicMock()
    factory.create_team = MagicMock(side_effect=[ValueError("nenhum membro"), "team-ok"])
    use_case = GetActiveTeamsUseCase(
        team_factory_service=factory,
        team_config_repository=InMemoryTeamConfigRepository([_make_team_config("t1"), _make_team_config("t2")]),
        logger=logger,
    )

    assert await use_case.execute([]) == ["team-ok"]
    errors = [(r.message, r.context) for r in logger.records if r.level == "error"]
    assert errors == [("Team não carregado", {"team_id": "t1", "error_type": "ValueError"})]


async def test_id_de_team_repetido_fica_com_o_primeiro_e_o_repetido_vira_log_de_erro():
    from tests.fakes import RecordingLogger

    logger = RecordingLogger()
    first, repeated = _make_team_config("dup"), _make_team_config("dup")
    first.nome, repeated.nome = "primeiro", "repetido"
    repository = MagicMock()
    repository.get_active_teams = AsyncMock(return_value=[first, _make_team_config("outro"), repeated])
    factory = MagicMock()
    factory.create_team = MagicMock(side_effect=lambda config, agents: f"team:{config.id}:{config.nome}")
    use_case = GetActiveTeamsUseCase(team_factory_service=factory, team_config_repository=repository, logger=logger)

    assert await use_case.execute([]) == ["team:dup:primeiro", "team:outro:Team outro"]
    assert factory.create_team.call_count == 2
    assert [(r.message, r.context) for r in logger.records if r.level == "error"] == [
        ("Team com id repetido ignorado", {"team_id": "dup"})
    ]


async def test_team_e_criado_fora_do_event_loop():
    """F2-02: criar o modelo do team pode ler segredo (``file:``) e resolver DNS do destino."""
    from tests.fakes import InMemoryTeamConfigRepository, RecordingLogger, running_on_event_loop

    on_loop: list[bool] = []
    factory = MagicMock()
    factory.create_team = MagicMock(side_effect=lambda config, agents: on_loop.append(running_on_event_loop()) or "t")
    use_case = GetActiveTeamsUseCase(
        team_factory_service=factory,
        team_config_repository=InMemoryTeamConfigRepository([_make_team_config("t1")]),
        logger=RecordingLogger(),
    )

    assert await use_case.execute([]) == ["t"]
    assert on_loop == [False]


async def test_config_de_modelo_recusada_vai_ao_log_com_o_motivo():
    """Como no use case de agentes: ``InvalidModelConfigError`` é texto nosso e vira ``reason``."""
    from src.domain.ports import InvalidModelConfigError
    from tests.fakes import InMemoryTeamConfigRepository, RecordingLogger

    logger = RecordingLogger()
    reason = "modelo do provider 'openai': host da base_url fora da allowlist"
    factory = MagicMock()
    factory.create_team = MagicMock(side_effect=InvalidModelConfigError(reason))
    use_case = GetActiveTeamsUseCase(
        team_factory_service=factory,
        team_config_repository=InMemoryTeamConfigRepository([_make_team_config("t1")]),
        logger=logger,
    )

    assert await use_case.execute([]) == []
    assert [(r.message, r.context) for r in logger.records if r.level == "error"] == [
        ("Team não carregado", {"team_id": "t1", "error_type": "InvalidModelConfigError", "reason": reason})
    ]
