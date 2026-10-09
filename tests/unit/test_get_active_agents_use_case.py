"""Testes para GetActiveAgentsUseCase contra o runtime falso (porta ``AgentRuntime``, F2-04)."""

from __future__ import annotations

import asyncio

from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase
from src.domain.entities.agent_config import AgentConfig
from src.domain.ports import InvalidModelConfigError
from tests.fakes import InMemoryAgentConfigRepository, RecordingLogger
from tests.fakes.runtime import FakeAgentHandle, FakeAgentRuntime


def _make_config(agent_id: str = "a1", nome: str = "Agente") -> AgentConfig:
    return AgentConfig(
        id=agent_id,
        nome=nome,
        factory_ia_model="ollama",
        model="llama3.2:latest",
        descricao="desc",
        prompt="prompt",
    )


class _ListRepository:
    """Repositório que devolve a lista como veio (o em memória indexa por id e não repete)."""

    def __init__(self, configs: list[AgentConfig]) -> None:
        self._configs = configs

    async def get_active_agents(self) -> list[AgentConfig]:
        return list(self._configs)


def _errors(logger: RecordingLogger) -> list[tuple[str, dict[str, object]]]:
    return [(r.message, r.context) for r in logger.records if r.level == "error"]


async def test_cada_config_ativa_vira_um_agente_na_ordem_do_repositorio():
    runtime = FakeAgentRuntime()
    repository = InMemoryAgentConfigRepository([_make_config("a1"), _make_config("a2")])
    use_case = GetActiveAgentsUseCase(runtime, repository, RecordingLogger())

    agents = await use_case.execute()

    assert agents == [FakeAgentHandle("a1", "Agente"), FakeAgentHandle("a2", "Agente")]
    assert [config.id for config in runtime.agent_builds] == ["a1", "a2"]


async def test_sem_config_ativa_nao_monta_nada():
    runtime = FakeAgentRuntime()
    use_case = GetActiveAgentsUseCase(runtime, InMemoryAgentConfigRepository(), RecordingLogger())

    assert await use_case.execute() == []
    assert runtime.agent_builds == []


async def test_falha_ao_criar_um_agente_vira_log_de_erro_com_id_e_tipo_e_os_outros_sobem():
    logger = RecordingLogger()
    repository = InMemoryAgentConfigRepository([_make_config("ok"), _make_config("quebra"), _make_config("ok-2")])
    runtime = FakeAgentRuntime(failures={"quebra": RuntimeError("sk-segredo-do-sdk")})
    use_case = GetActiveAgentsUseCase(runtime, repository, logger)

    agents = await use_case.execute()

    assert [agent.id for agent in agents] == ["ok", "ok-2"]
    assert _errors(logger) == [("Agente não carregado", {"agent_id": "quebra", "error_type": "RuntimeError"})]
    assert all("sk-segredo-do-sdk" not in repr(r) for r in logger.records)


async def test_task_de_criacao_cancelada_nao_vira_agente():
    """``gather(return_exceptions=True)`` devolve ``CancelledError`` (BaseException) como resultado."""
    logger = RecordingLogger()
    repository = InMemoryAgentConfigRepository([_make_config("ok"), _make_config("cancelado")])
    runtime = FakeAgentRuntime(failures={"cancelado": asyncio.CancelledError()})
    use_case = GetActiveAgentsUseCase(runtime, repository, logger)

    agents = await use_case.execute()

    assert [agent.id for agent in agents] == ["ok"]
    assert _errors(logger) == [("Agente não carregado", {"agent_id": "cancelado", "error_type": "CancelledError"})]


async def test_id_repetido_fica_com_o_primeiro_e_o_repetido_vira_log_de_erro():
    """O AgentOS recusa ids repetidos ("Duplicate IDs") e todos os agentes ficavam sem rota."""
    logger = RecordingLogger()
    runtime = FakeAgentRuntime()
    configs = [_make_config("dup", "primeiro"), _make_config("outro"), _make_config("dup", "repetido")]
    use_case = GetActiveAgentsUseCase(runtime, _ListRepository(configs), logger)

    agents = await use_case.execute()

    assert agents == [FakeAgentHandle("dup", "primeiro"), FakeAgentHandle("outro", "Agente")]
    assert [config.nome for config in runtime.agent_builds] == ["primeiro", "Agente"]
    assert _errors(logger) == [("Agente com id repetido ignorado", {"agent_id": "dup"})]


async def test_config_de_modelo_invalida_loga_o_motivo_que_e_texto_nosso():
    """``InvalidModelConfigError`` traz só texto da validação (como ``DocumentPathError``): vai como
    ``reason``; nas demais exceções, só o tipo."""
    logger = RecordingLogger()
    reason = "Configuração de modelo inválida: Tipo 'xyz' não suportado"
    runtime = FakeAgentRuntime(failures={"ruim": InvalidModelConfigError(reason)})
    use_case = GetActiveAgentsUseCase(runtime, InMemoryAgentConfigRepository([_make_config("ruim")]), logger)

    assert await use_case.execute() == []
    assert _errors(logger) == [
        ("Agente não carregado", {"agent_id": "ruim", "error_type": "InvalidModelConfigError", "reason": reason})
    ]


async def test_agentes_sao_montados_em_paralelo():
    """Um agente lento (RAG, tools) não atrasa a montagem dos outros: todos começam antes de algum terminar."""
    started: list[str] = []
    release = asyncio.Event()

    class _SlowRuntime(FakeAgentRuntime):
        async def build_agent(self, config: AgentConfig) -> FakeAgentHandle:
            started.append(config.id)
            if len(started) == 2:
                release.set()
            await asyncio.wait_for(release.wait(), timeout=1)
            return await super().build_agent(config)

    use_case = GetActiveAgentsUseCase(
        _SlowRuntime(), InMemoryAgentConfigRepository([_make_config("a1"), _make_config("a2")]), RecordingLogger()
    )

    assert [agent.id for agent in await use_case.execute()] == ["a1", "a2"]
    assert started == ["a1", "a2"]
