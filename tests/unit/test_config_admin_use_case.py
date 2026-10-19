"""FA-02: caso de uso de administração da config sobre os repositórios em memória e os mappers reais."""

from __future__ import annotations

import asyncio
import dataclasses
import math

import pytest

from src.application.use_cases.config_admin_use_case import (
    AgentConfigEditor,
    ConfigAlreadyExists,
    ConfigInUse,
    ConfigNotFound,
    InvalidConfig,
)
from src.domain.entities.agent_config import AgentConfig
from src.domain.repositories.versioning import ConfigVersionConflict, Versioned
from src.infrastructure.dependency_injection import build_config_admin
from src.infrastructure.repositories.config_documents import entity_document
from tests.fakes.admin_config_app import agent, agent_body, team, tool
from tests.fakes.logger import RecordingLogger
from tests.fakes.repositories import (
    InMemoryAgentConfigRepository,
    InMemoryTeamConfigRepository,
    InMemoryToolRepository,
)


class _LosesCreationRace(InMemoryAgentConfigRepository):
    """Outro processo criou o mesmo id entre a leitura e a gravação."""

    async def save(self, entity: AgentConfig, expected_version: int) -> int:
        raise ConfigVersionConflict(entity.id, expected_version)


class _YieldingAgents(InMemoryAgentConfigRepository):
    """Cede o loop em cada operação, como um driver real: abre espaço para intercalar requests."""

    async def list_all(self) -> list[Versioned[AgentConfig]]:
        await asyncio.sleep(0)
        return await super().list_all()

    async def save(self, entity: AgentConfig, expected_version: int) -> int:
        await asyncio.sleep(0)
        return await super().save(entity, expected_version)

    async def delete(self, entity_id: str, expected_version: int) -> None:
        await asyncio.sleep(0)
        await super().delete(entity_id, expected_version)


def _admin(
    agents: InMemoryAgentConfigRepository | None = None,
    logger: RecordingLogger | None = None,
    *,
    teams: InMemoryTeamConfigRepository | None = None,
    tools: InMemoryToolRepository | None = None,
):
    return build_config_admin(
        agents or InMemoryAgentConfigRepository(),
        teams or InMemoryTeamConfigRepository(),
        tools or InMemoryToolRepository(),
        logger or RecordingLogger(),
    )


def _refusals(logger: RecordingLogger) -> list[tuple[str, dict[str, object]]]:
    return [(r.level, r.context) for r in logger.records]


async def test_criacao_que_perde_a_corrida_vira_ja_existe():
    admin = _admin(_LosesCreationRace())

    with pytest.raises(ConfigAlreadyExists):
        await admin.agents.create(agent_body("a1"))


async def test_documento_recusado_pela_entidade_diz_o_campo_e_loga_so_o_campo_e_o_tipo_de_config():
    logger = RecordingLogger()
    admin = _admin(logger=logger)

    with pytest.raises(InvalidConfig) as caught:
        await admin.agents.create(agent_body("a1", nome=["valor-recusado"]))

    assert (caught.value.field, caught.value.indexes) == ("nome", ())
    assert str(caught.value) == "Configuração inválida"
    assert _refusals(logger) == [("warning", {"kind": "agents", "refusal": "InvalidConfig", "field": "nome"})]


async def test_recusa_do_mapper_sem_campo_conhecido_vira_422_sem_campo():
    def parse(_: object) -> AgentConfig:
        raise TypeError("detalhe que cita o valor")

    logger = RecordingLogger()
    repos = (InMemoryTeamConfigRepository(), InMemoryToolRepository())
    editor = AgentConfigEditor(
        InMemoryAgentConfigRepository(), parse, entity_document, logger, asyncio.Lock(), tools=repos[1], teams=repos[0]
    )

    with pytest.raises(InvalidConfig) as caught:
        await editor.create(agent_body("a1"))

    assert (caught.value.field, caught.value.indexes) == (None, ())
    assert "valor" not in repr(logger.records)


async def test_item_de_tools_ids_que_nao_e_texto_e_recusado_na_posicao_do_corpo_sem_log_de_erro():
    """O mapper da leitura descartava o 123 e a posição do id inexistente saía deslocada (1 em vez de 2)."""
    logger = RecordingLogger()
    admin = _admin(logger=logger, tools=InMemoryToolRepository([tool("cep")]))

    with pytest.raises(InvalidConfig) as caught:
        await admin.agents.create(agent_body("a1", tools_ids=[123, "cep", "fantasma"]))

    assert (caught.value.field, caught.value.indexes) == ("tools_ids", (0,))
    assert [r.level for r in logger.records] == ["warning"]


async def test_tools_ids_inexistente_aponta_a_posicao_no_corpo():
    admin = _admin(tools=InMemoryToolRepository([tool("cep")]))

    with pytest.raises(InvalidConfig) as caught:
        await admin.agents.create(agent_body("a1", tools_ids=["cep", "fantasma"]))

    assert (caught.value.field, caught.value.indexes) == ("tools_ids", (1,))


async def test_chave_de_operador_em_qualquer_nivel_e_recusada_com_log_sem_a_chave():
    logger = RecordingLogger()
    agents = InMemoryAgentConfigRepository()
    admin = _admin(agents, logger)

    with pytest.raises(InvalidConfig) as caught:
        await admin.agents.create(agent_body("a1", rag_config={"active": True, "nested": [{"$ne": 1}]}))

    assert caught.value.field is None
    assert _refusals(logger) == [("warning", {"kind": "agents", "refusal": "InvalidConfig", "field": None})]
    assert await agents.list_all() == []


@pytest.mark.parametrize(
    "unserializable",
    [
        {"model_params": {"temperature": float("nan")}},
        {"rag_config": {"active": True, "model": "m", "factory_ia_model": "ollama", "model_params": {"d": -math.inf}}},
        {"descricao": "texto \ud800 com surrogate"},
        {"model_params": {"\udfff": 1}},
    ],
    ids=["nan", "infinito-aninhado", "surrogate-em-valor", "surrogate-em-chave"],
)
async def test_valor_que_o_json_da_resposta_nao_serializa_e_recusado_sem_gravar(unserializable: dict[str, object]):
    """NaN/infinito e surrogate solitário seriam gravados e quebrariam toda resposta que mostra o documento."""
    logger = RecordingLogger()
    agents = InMemoryAgentConfigRepository()
    admin = _admin(agents, logger)

    with pytest.raises(InvalidConfig) as caught:
        await admin.agents.create(agent_body("a1", **unserializable))

    assert caught.value.field is None
    assert await agents.list_all() == []


async def test_recusas_de_conflito_versao_e_uso_logam_o_tipo_sem_id():
    logger = RecordingLogger()
    admin = _admin(
        InMemoryAgentConfigRepository([agent("a1")]),
        logger,
        teams=InMemoryTeamConfigRepository([team("t1", ["a1"])]),
    )

    for attempt in (
        admin.agents.create(agent_body("a1")),
        admin.agents.replace("a1", agent_body("a1"), 4),
        admin.agents.remove("a1", 0),
    ):
        with pytest.raises((ConfigAlreadyExists, ConfigVersionConflict, ConfigInUse)):
            await attempt

    assert _refusals(logger) == [
        ("warning", {"kind": "agents", "refusal": refusal, "field": None})
        for refusal in ("ConfigAlreadyExists", "ConfigVersionConflict", "ConfigInUse")
    ]
    assert "a1" not in repr(logger.records)


async def test_mascara_com_troca_de_origem_da_route_e_recusada():
    """Com ``***`` mantido, apontar a tool para outro host entregaria o segredo gravado a ele."""
    secret_header = {"Authorization": "Bearer valor-de-teste"}
    tools = InMemoryToolRepository([tool("cep", headers=secret_header)])
    admin = _admin(tools=tools)
    shown = (await admin.tools.get("cep")).entity

    for route in (
        "https://outro.example.invalid/cep/{cep}",
        "http://api.example.invalid/cep/{cep}",
        "https://api.example.invalid:8443/cep/{cep}",
        "https://api.example.invalid:99999/cep/{cep}",  # porta que nem parseia
    ):
        with pytest.raises(InvalidConfig) as caught:
            await admin.tools.replace("cep", {**shown, "route": route}, 0)
        assert caught.value.field == "headers"
    updated = await admin.tools.replace("cep", {**shown, "route": "https://api.example.invalid/v2/{cep}"}, 0)
    with_new_value = await admin.tools.replace(
        "cep", {**shown, "route": "https://outro.example.invalid/x", "headers": {"Authorization": "novo"}}, 1
    )

    assert updated.version == 1 and with_new_value.version == 2
    assert [i.entity.headers for i in await tools.list_all()] == [{"Authorization": "novo"}]


async def test_alteracao_com_id_repetido_valida_e_mantem_o_header_do_documento_da_versao_do_if_match():
    first = tool("cep", headers={"Authorization": "do-primeiro"})
    second = dataclasses.replace(tool("cep", headers={"Authorization": "do-segundo"}), description="segundo")
    tools = InMemoryToolRepository([first, second])
    await tools.save(first, 0)  # primeiro na versão 1, segundo segue na 0
    admin = _admin(tools=tools)
    masked = {**(await admin.tools.get("cep")).entity, "description": "alterado"}

    replaced = await admin.tools.replace("cep", masked, 0)
    with pytest.raises(ConfigVersionConflict):
        await admin.tools.replace("cep", masked, 5)
    with pytest.raises(ConfigNotFound):
        await admin.tools.replace("outro", masked, 0)

    assert replaced.version == 1
    stored = [(i.entity.description, i.entity.headers, i.version) for i in await tools.list_all()]
    assert stored == [
        ("Busca endereço", {"Authorization": "do-primeiro"}, 1),
        ("alterado", {"Authorization": "do-segundo"}, 1),
    ]


async def test_remocao_concorrente_com_alteracao_de_legado_nao_ressuscita_o_documento():
    """Sem a trava, o ``save(…, 0)`` da alteração viraria ``insert`` depois da remoção."""
    agents = _YieldingAgents([agent("a1")])
    admin = _admin(agents)

    results = await asyncio.gather(
        admin.agents.remove("a1", 0),
        admin.agents.replace("a1", agent_body("a1", prompt="alterado"), 0),
        return_exceptions=True,
    )

    assert results[0] is None
    assert isinstance(results[1], ConfigNotFound)
    assert await agents.list_all() == []


async def test_criacao_e_remocao_concorrentes_nao_deixam_referencia_para_tool_removida():
    tools = InMemoryToolRepository([tool("cep")])
    admin = _admin(_YieldingAgents(), tools=tools)

    results = await asyncio.gather(
        admin.agents.create(agent_body("a1", tools_ids=["cep"])),
        admin.tools.remove("cep", 0),
        return_exceptions=True,
    )

    assert isinstance(results[0], Versioned)
    assert isinstance(results[1], ConfigInUse)
    assert [i.entity.id for i in await tools.list_all()] == ["cep"]


async def test_criacao_grava_a_versao_1_e_devolve_o_documento_em_snake_case():
    agents = InMemoryAgentConfigRepository()
    admin = _admin(agents)

    body = agent_body("a1")
    body["factoryIaModel"] = body.pop("factory_ia_model").replace("ollama", "openai")
    created = await admin.agents.create(body)

    assert created.version == 1
    assert created.entity["factory_ia_model"] == "openai"
    assert "factoryIaModel" not in created.entity
    assert [(i.entity.factory_ia_model, i.version) for i in await agents.list_all()] == [("openai", 1)]
