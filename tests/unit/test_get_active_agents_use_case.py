"""Testes para GetActiveAgentsUseCase."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock

from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase
from src.domain.entities.agent_config import AgentConfig
from tests.fakes import InMemoryAgentConfigRepository, RecordingLogger


def _make_config(agent_id: str = "a1") -> AgentConfig:
    return AgentConfig(
        id=agent_id,
        nome="Agente",
        factory_ia_model="ollama",
        model="llama3.2:latest",
        descricao="desc",
        prompt="prompt",
    )


class TestGetActiveAgentsUseCase:
    async def test_execute_returns_agents(self, mock_agent_config_repository):
        configs = [_make_config("a1"), _make_config("a2")]
        mock_agent_config_repository.get_active_agents.return_value = configs

        mock_factory = AsyncMock()
        mock_agent = MagicMock()
        mock_factory.create_agent = AsyncMock(return_value=mock_agent)

        use_case = GetActiveAgentsUseCase(mock_factory, mock_agent_config_repository, RecordingLogger())
        agents = await use_case.execute()

        assert len(agents) == 2
        assert mock_factory.create_agent.await_count == 2

    async def test_execute_returns_empty_when_no_configs(self, mock_agent_config_repository):
        mock_agent_config_repository.get_active_agents.return_value = []
        mock_factory = AsyncMock()

        use_case = GetActiveAgentsUseCase(mock_factory, mock_agent_config_repository, RecordingLogger())
        agents = await use_case.execute()

        assert agents == []
        mock_factory.create_agent.assert_not_awaited()

    async def test_execute_skips_failed_agents(self, mock_agent_config_repository):
        configs = [_make_config("ok"), _make_config("fail")]
        mock_agent_config_repository.get_active_agents.return_value = configs

        mock_factory = AsyncMock()
        mock_agent = MagicMock()
        mock_factory.create_agent = AsyncMock(
            side_effect=[mock_agent, RuntimeError("boom")]
        )

        use_case = GetActiveAgentsUseCase(mock_factory, mock_agent_config_repository, RecordingLogger())
        agents = await use_case.execute()

        assert len(agents) == 1


class _FailingFactory:
    """Cria um "agente" por config; falha nos ids pedidos com a exceção dada."""

    def __init__(self, failures: dict[str, BaseException]) -> None:
        self._failures = failures

    async def create_agent(self, config: AgentConfig) -> str:
        if config.id in self._failures:
            raise self._failures[config.id]
        return f"agente:{config.id}"


async def test_falha_ao_criar_um_agente_vira_log_de_erro_com_id_e_tipo_e_os_outros_sobem():
    logger = RecordingLogger()
    repository = InMemoryAgentConfigRepository([_make_config("ok"), _make_config("quebra"), _make_config("ok-2")])
    factory = _FailingFactory({"quebra": RuntimeError("sk-segredo-do-sdk")})
    use_case = GetActiveAgentsUseCase(factory, repository, logger)  # type: ignore[arg-type]

    agents = await use_case.execute()

    assert agents == ["agente:ok", "agente:ok-2"]
    errors = [(r.message, r.context) for r in logger.records if r.level == "error"]
    assert errors == [("Agente não carregado", {"agent_id": "quebra", "error_type": "RuntimeError"})]
    assert all("sk-segredo-do-sdk" not in repr(r) for r in logger.records)


async def test_task_de_criacao_cancelada_nao_vira_agente():
    """``gather(return_exceptions=True)`` devolve ``CancelledError`` (BaseException) como resultado."""
    import asyncio

    logger = RecordingLogger()
    repository = InMemoryAgentConfigRepository([_make_config("ok"), _make_config("cancelado")])
    factory = _FailingFactory({"cancelado": asyncio.CancelledError()})
    use_case = GetActiveAgentsUseCase(factory, repository, logger)  # type: ignore[arg-type]

    agents = await use_case.execute()

    assert agents == ["agente:ok"]
    assert [(r.message, r.context) for r in logger.records if r.level == "error"] == [
        ("Agente não carregado", {"agent_id": "cancelado", "error_type": "CancelledError"})
    ]


class _ListRepository:
    """Repositório que devolve a lista como veio (o em memória indexa por id e não repete)."""

    def __init__(self, configs: list[AgentConfig]) -> None:
        self._configs = configs

    async def get_active_agents(self) -> list[AgentConfig]:
        return list(self._configs)


async def test_id_repetido_fica_com_o_primeiro_e_o_repetido_vira_log_de_erro():
    """O AgentOS recusa ids repetidos ("Duplicate IDs") e todos os agentes ficavam sem rota."""
    logger = RecordingLogger()
    first, repeated = _make_config("dup"), _make_config("dup")
    first.nome, repeated.nome = "primeiro", "repetido"
    created: list[str] = []

    class _Factory:
        async def create_agent(self, config: AgentConfig) -> str:
            created.append(config.nome)
            return f"agente:{config.id}:{config.nome}"

    use_case = GetActiveAgentsUseCase(
        _Factory(), _ListRepository([first, _make_config("outro"), repeated]), logger  # type: ignore[arg-type]
    )

    agents = await use_case.execute()

    assert agents == ["agente:dup:primeiro", "agente:outro:Agente"]
    assert created == ["primeiro", "Agente"]
    assert [(r.message, r.context) for r in logger.records if r.level == "error"] == [
        ("Agente com id repetido ignorado", {"agent_id": "dup"})
    ]


async def test_config_de_modelo_invalida_loga_o_motivo_que_e_texto_nosso():
    """``InvalidModelConfigError`` traz só texto da validação (como ``DocumentPathError``): vai como
    ``reason``; nas demais exceções, só o tipo."""
    from src.domain.ports.model_factory_port import InvalidModelConfigError

    logger = RecordingLogger()
    reason = "Configuração de modelo inválida: Tipo 'xyz' não suportado"
    factory = _FailingFactory({"ruim": InvalidModelConfigError(reason)})
    use_case = GetActiveAgentsUseCase(
        factory, InMemoryAgentConfigRepository([_make_config("ruim")]), logger  # type: ignore[arg-type]
    )

    assert await use_case.execute() == []
    assert [(r.message, r.context) for r in logger.records if r.level == "error"] == [
        ("Agente não carregado", {"agent_id": "ruim", "error_type": "InvalidModelConfigError", "reason": reason})
    ]
