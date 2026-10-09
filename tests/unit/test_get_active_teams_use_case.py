"""Testes para GetActiveTeamsUseCase contra o runtime falso (porta ``AgentRuntime``, F2-04)."""

from __future__ import annotations

from src.application.use_cases.get_active_teams_use_case import GetActiveTeamsUseCase
from src.domain.entities.team_config import TeamConfig
from src.domain.ports import InvalidModelConfigError
from tests.fakes import InMemoryTeamConfigRepository, RecordingLogger
from tests.fakes.runtime import FakeAgentHandle, FakeAgentRuntime, FakeTeamHandle


def _make_team_config(team_id: str = "team-1", nome: str | None = None) -> TeamConfig:
    return TeamConfig(
        id=team_id,
        nome=nome or f"Team {team_id}",
        factory_ia_model="ollama",
        model="llama3.2:latest",
        member_ids=["agent-a"],
        mode="route",
    )


class _ListRepository:
    """Repositório que devolve a lista como veio (o em memória indexa por id e não repete)."""

    def __init__(self, configs: list[TeamConfig]) -> None:
        self._configs = configs

    async def get_active_teams(self) -> list[TeamConfig]:
        return list(self._configs)


def _errors(logger: RecordingLogger) -> list[tuple[str, dict[str, object]]]:
    return [(r.message, r.context) for r in logger.records if r.level == "error"]


AGENTS = [FakeAgentHandle("agent-a", "A"), FakeAgentHandle("agent-b", "B")]


async def test_cada_config_ativa_vira_um_team_com_os_agentes_recebidos():
    runtime = FakeAgentRuntime()
    repository = InMemoryTeamConfigRepository([_make_team_config("t1"), _make_team_config("t2")])
    use_case = GetActiveTeamsUseCase(runtime, repository, RecordingLogger())

    teams = await use_case.execute(AGENTS)

    assert teams == [FakeTeamHandle("t1", "Team t1", ("agent-a",)), FakeTeamHandle("t2", "Team t2", ("agent-a",))]
    assert [build.agent_ids for build in runtime.team_builds] == [("agent-a", "agent-b")] * 2


async def test_sem_config_ativa_nao_monta_nada():
    runtime = FakeAgentRuntime()
    use_case = GetActiveTeamsUseCase(runtime, InMemoryTeamConfigRepository(), RecordingLogger())

    assert await use_case.execute(AGENTS) == []
    assert runtime.team_builds == []


async def test_falha_ao_criar_um_team_vira_log_de_erro_com_id_e_tipo_e_os_outros_sobem():
    logger = RecordingLogger()
    runtime = FakeAgentRuntime(failures={"t1": ValueError("sk-segredo-do-sdk")})
    repository = InMemoryTeamConfigRepository([_make_team_config("t1"), _make_team_config("t2")])
    use_case = GetActiveTeamsUseCase(runtime, repository, logger)

    assert [team.id for team in await use_case.execute(AGENTS)] == ["t2"]
    assert _errors(logger) == [("Team não carregado", {"team_id": "t1", "error_type": "ValueError"})]
    assert all("sk-segredo-do-sdk" not in repr(r) for r in logger.records)


async def test_todos_falham_devolve_lista_vazia():
    runtime = FakeAgentRuntime(failures={"t1": RuntimeError("fail")})
    use_case = GetActiveTeamsUseCase(
        runtime, InMemoryTeamConfigRepository([_make_team_config("t1")]), RecordingLogger()
    )

    assert await use_case.execute(AGENTS) == []


async def test_id_de_team_repetido_fica_com_o_primeiro_e_o_repetido_vira_log_de_erro():
    logger = RecordingLogger()
    runtime = FakeAgentRuntime()
    configs = [_make_team_config("dup", "primeiro"), _make_team_config("outro"), _make_team_config("dup", "repetido")]
    use_case = GetActiveTeamsUseCase(runtime, _ListRepository(configs), logger)

    teams = await use_case.execute(AGENTS)

    assert [(team.id, team.nome) for team in teams] == [("dup", "primeiro"), ("outro", "Team outro")]
    assert len(runtime.team_builds) == 2
    assert _errors(logger) == [("Team com id repetido ignorado", {"team_id": "dup"})]


async def test_team_e_criado_fora_do_event_loop():
    """F2-02: criar o modelo do team pode ler segredo (``file:``) e resolver DNS do destino."""
    runtime = FakeAgentRuntime()
    use_case = GetActiveTeamsUseCase(
        runtime, InMemoryTeamConfigRepository([_make_team_config("t1")]), RecordingLogger()
    )

    assert [team.id for team in await use_case.execute(AGENTS)] == ["t1"]
    assert [build.on_event_loop for build in runtime.team_builds] == [False]


async def test_config_de_modelo_recusada_vai_ao_log_com_o_motivo():
    """Como no use case de agentes: ``InvalidModelConfigError`` é texto nosso e vira ``reason``."""
    logger = RecordingLogger()
    reason = "modelo do provider 'openai': host da base_url fora da allowlist"
    runtime = FakeAgentRuntime(failures={"t1": InvalidModelConfigError(reason)})
    use_case = GetActiveTeamsUseCase(runtime, InMemoryTeamConfigRepository([_make_team_config("t1")]), logger)

    assert await use_case.execute(AGENTS) == []
    assert _errors(logger) == [
        ("Team não carregado", {"team_id": "t1", "error_type": "InvalidModelConfigError", "reason": reason})
    ]
