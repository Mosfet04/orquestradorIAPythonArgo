"""FA-05: auditoria de cada escrita, histórico, restauração e o poll da revisão do store, no caso de uso de config.

Repositórios em memória, runtime falso e o diário do container (``MongoConfigAuditLog``) sobre o Mongo
falso. Duas réplicas = dois ``ConfigAdmin`` sobre os mesmos repositórios e o mesmo database do diário, cada
uma com o seu runtime. A pilha completa (dois apps, ``AgnoRuntime``) está em
``tests/integration/test_fa05_replicas.py``.
"""

from __future__ import annotations

import pytest

from src.application.use_cases.config_admin_use_case import ConfigAdmin, ConfigNotFound, InvalidConfig
from src.domain.entities.agent_config import AgentConfig
from src.domain.repositories.config_audit import ConfigAuditEntry, FieldChange
from src.domain.repositories.versioning import ConfigStoreReadOnly
from src.infrastructure.dependency_injection import build_config_admin
from src.infrastructure.repositories.mongo_config_audit_log import MongoConfigAuditLog
from tests.fakes.admin_config_app import agent_body, fake_audit_log, fake_runtime_swap, route_guard, tool, tool_body
from tests.fakes.logger import RecordingLogger
from tests.fakes.mongo import FakeMotorClient
from tests.fakes.repositories import (
    InMemoryAgentConfigRepository,
    InMemoryTeamConfigRepository,
    InMemoryToolRepository,
)
from tests.fakes.runtime import FakeAgentRuntime

ACTOR = "admin"


class _Store:
    """Repositórios e database do diário compartilhados pelas réplicas (o mesmo Mongo)."""

    def __init__(self, tools: InMemoryToolRepository | None = None, agents: list[AgentConfig] | None = None) -> None:
        self.agents = _FlakyAgents(agents or [])
        self.teams = InMemoryTeamConfigRepository()
        self.tools = tools or InMemoryToolRepository()
        self.database = FakeMotorClient()["agno"]


class _FlakyAgents(InMemoryAgentConfigRepository):
    """Com ``failing``, a leitura dos ativos (a montagem do reload) falha como o Mongo fora."""

    failing = False

    async def get_active_agents(self) -> list[AgentConfig]:
        if self.failing:
            raise ConnectionError("mongo fora")
        return await super().get_active_agents()


class _Replica:
    def __init__(self, store: _Store, audit: MongoConfigAuditLog | None = None) -> None:
        self.runtime = FakeAgentRuntime()
        self.logger = RecordingLogger()
        self.admin: ConfigAdmin = build_config_admin(
            store.agents,
            store.teams,
            store.tools,
            self.logger,
            runtime=fake_runtime_swap(store.agents, store.teams, self.runtime),
            audit=audit or fake_audit_log(store.database),
            route_guard=route_guard(),
        )

    def served(self) -> list[tuple[str, str]]:
        agents, _ = self.runtime.served
        return [(a.id, a.nome) for a in agents]  # type: ignore[attr-defined]


class _FailingRecord(MongoConfigAuditLog):
    async def record(self, entry: ConfigAuditEntry) -> int:
        raise ConnectionError("mongo fora")


# ── auditoria ───────────────────────────────────────────────────────


async def test_cada_escrita_registra_ator_tipo_id_versao_e_so_o_que_mudou():
    store = _Store()
    replica = _Replica(store)
    agents = replica.admin.agents

    await agents.create(agent_body("a1"), actor=ACTOR)
    await agents.replace("a1", agent_body("a1", prompt="novo"), 1, actor="local-dev")
    await agents.deactivate("a1", 2, actor=ACTOR)
    await agents.remove("a1", 3, actor=ACTOR)

    history = await agents.history("a1")
    assert [(e.kind, e.entity_id, e.version, e.actor) for e in history] == [
        ("agents", "a1", 3, ACTOR),
        ("agents", "a1", 3, ACTOR),
        ("agents", "a1", 2, "local-dev"),
        ("agents", "a1", 1, ACTOR),
    ]
    removed, deactivated, replaced, created = history
    assert replaced.diff == {"prompt": FieldChange("p", "novo")}
    assert deactivated.diff == {"active": FieldChange(True, False)}
    assert created.diff["id"] == FieldChange(None, "a1") and removed.diff["id"] == FieldChange("a1", None)
    assert removed.document is None
    assert deactivated.document is not None and deactivated.document["active"] is False
    assert created.at.tzinfo is not None and created.at <= removed.at
    assert await fake_audit_log(store.database).revision() == 4


async def test_header_de_tool_sai_mascarado_no_documento_e_no_diff_e_a_troca_do_valor_aparece():
    store = _Store()
    tools = _Replica(store).admin.tools
    body = tool_body(headers={"X-Cliente": "valor-um"})  # nome não sensível: texto novo aceito (FA-06)

    await tools.create(body, actor=ACTOR)
    await tools.replace("cep", {**body, "headers": {"X-Cliente": "valor-dois"}}, 1, actor=ACTOR)

    changed, created = await tools.history("cep")
    masked = {"X-Cliente": "***"}
    assert changed.diff == {"headers": FieldChange(masked, masked)}
    assert created.document is not None and created.document["headers"] == masked
    assert "valor-" not in repr(await tools.history("cep"))
    assert "valor-" not in repr(await store.database["config_audit"].find({}).to_list(length=None))


# ── restauração ─────────────────────────────────────────────────────


async def test_restaurar_versao_monta_grava_a_versao_seguinte_e_troca_o_runtime():
    store = _Store()
    replica = _Replica(store)
    agents = replica.admin.agents
    await agents.create(agent_body("a1", nome="Um"), actor=ACTOR)
    await agents.replace("a1", agent_body("a1", nome="Dois"), 1, actor=ACTOR)

    restored = await agents.restore("a1", 1, 2, actor=ACTOR)

    assert (restored.item.version, restored.item.entity["nome"]) == (3, "Um")
    assert replica.served() == [("a1", "Um")] and restored.runtime_revision == 3
    latest = (await agents.history("a1"))[0]
    assert (latest.version, latest.diff) == (3, {"nome": FieldChange("Dois", "Um")})


async def test_restaurar_passa_pela_validacao_das_referencias_e_recusa_sem_gravar():
    store = _Store(InMemoryToolRepository([tool("cep")]))
    replica = _Replica(store)
    await replica.admin.agents.create(agent_body("a1", tools_ids=["cep"]), actor=ACTOR)
    await replica.admin.agents.replace("a1", agent_body("a1"), 1, actor=ACTOR)
    await replica.admin.tools.remove("cep", 0, actor=ACTOR)

    with pytest.raises(InvalidConfig) as caught:
        await replica.admin.agents.restore("a1", 1, 2, actor=ACTOR)

    assert caught.value.field == "tools_ids"
    assert [item.version for item in await store.agents.list_all()] == [2]
    assert [e.version for e in await replica.admin.agents.history("a1")] == [2, 1]


async def test_restaurar_versao_fora_do_historico_ou_so_a_remocao_e_nao_encontrada():
    store = _Store(agents=[_agent("legado")])  # versão 0, sem entrada de criação
    agents = _Replica(store).admin.agents
    await agents.create(agent_body("a1"), actor=ACTOR)
    await agents.remove("legado", 0, actor=ACTOR)

    for entity_id, version in (("a1", 7), ("fantasma", 1), ("legado", 0)):
        with pytest.raises(ConfigNotFound):
            await agents.restore(entity_id, version, 1, actor=ACTOR)


async def test_restaurar_tool_mantem_o_valor_gravado_dos_headers_so_na_mesma_origem():
    """O histórico não tem o valor do header: a versão restaurada fica com o gravado, como no ``PUT`` mascarado."""
    store = _Store(InMemoryToolRepository([tool("cep", headers={"Authorization": "Bearer gravado"})]))
    tools = _Replica(store).admin.tools
    shown = (await tools.get("cep")).entity
    await tools.replace("cep", {**shown, "description": "v1"}, 0, actor=ACTOR)
    await tools.replace("cep", {**shown, "description": "v2"}, 1, actor=ACTOR)

    await tools.restore("cep", 1, 2, actor=ACTOR)
    moved = {**shown, "route": "https://outro.example.invalid/x", "headers": {"X-Cliente": "Bearer outro"}}
    await tools.replace("cep", moved, 3, actor=ACTOR)
    with pytest.raises(InvalidConfig) as caught:
        await tools.restore("cep", 1, 4, actor=ACTOR)  # volta à route antiga com o valor do outro host

    assert caught.value.field == "headers"
    stored = [(i.entity.description, i.entity.headers, i.version) for i in await store.tools.list_all()]
    assert stored == [("Busca endereço", {"X-Cliente": "Bearer outro"}, 4)]
    assert [e.document["description"] for e in await tools.history("cep") if e.document][:2] == ["Busca endereço", "v1"]


async def test_store_sem_diario_e_so_leitura_historico_vazio_e_poll_desligado():
    """Store YAML: a API não grava (409), então não há histórico nem revisão a seguir; o reload é manual."""
    store = _Store()
    runtime = FakeAgentRuntime()
    admin = build_config_admin(
        store.agents, store.teams, store.tools, RecordingLogger(),
        runtime=fake_runtime_swap(store.agents, store.teams, runtime), audit=None, route_guard=route_guard(),
    )

    with pytest.raises(ConfigStoreReadOnly):
        await admin.agents.restore("a1", 1, 0, actor=ACTOR)
    await admin.track()

    assert await admin.agents.history("a1") == []
    assert await admin.sync() is None
    assert runtime.revision == 0


async def test_falha_ao_registrar_loga_so_o_tipo_sobe_e_nenhuma_replica_troca():
    """Sem registro não há revisão nova: todas as réplicas, a que escreveu inclusive, seguem na anterior."""
    store = _Store()
    replica = _Replica(store, audit=_FailingRecord(store.database))

    with pytest.raises(ConnectionError):
        await replica.admin.agents.create(agent_body("a1"), actor=ACTOR)

    assert [item.entity.id for item in await store.agents.list_all()] == ["a1"]
    assert replica.runtime.revision == 0
    errors = [(r.message, r.context) for r in replica.logger.records if r.level == "error"]
    assert errors == [
        (
            "Config gravada, mas o runtime não foi trocado; use POST /admin/config/reload",
            {"kind": "agents", "error_type": "ConnectionError"},
        )
    ]


# ── poll da revisão (propagação entre réplicas) ─────────────────────


async def test_outra_replica_serve_a_escrita_na_volta_seguinte_do_poll():
    store = _Store()
    writer, reader = _Replica(store), _Replica(store)
    await writer.admin.track()
    await reader.admin.track()

    await writer.admin.agents.create(agent_body("a1", nome="Novo"), actor=ACTOR)

    assert reader.served() == []
    assert await reader.admin.sync() == 1
    assert reader.served() == [("a1", "Novo")]
    assert await reader.admin.sync() is None
    assert await writer.admin.sync() is None  # a própria escrita já é servida: não remonta
    assert writer.runtime.revision == 1


async def test_escrita_com_outra_replica_no_meio_remonta_no_poll():
    """A montagem da escrita pode não ter visto a da outra réplica: o poll relê o store inteiro."""
    store = _Store()
    first, second = _Replica(store), _Replica(store)
    await first.admin.track()
    await second.admin.track()

    await second.admin.agents.create(agent_body("a2"), actor=ACTOR)
    await first.admin.agents.create(agent_body("a1"), actor=ACTOR)

    assert await first.admin.sync() == 2
    assert [agent_id for agent_id, _ in first.served()] == ["a2", "a1"]
    assert await first.admin.sync() is None


async def test_sem_revisao_do_startup_o_primeiro_poll_rele():
    """Mongo fora no startup: a revisão não foi lida, e a primeira volta que a lê remonta tudo."""
    store = _Store()
    reader = _Replica(store)
    await store.agents.save(_agent("a1"), 0)

    assert await reader.admin.sync() == 1
    assert reader.served() == [("a1", "Agente a1")]


async def test_poll_com_montagem_falha_sobe_e_tenta_de_novo_na_volta_seguinte():
    store = _Store()
    writer, reader = _Replica(store), _Replica(store)
    await reader.admin.track()
    await writer.admin.agents.create(agent_body("a1"), actor=ACTOR)

    store.agents.failing = True
    with pytest.raises(ConnectionError):
        await reader.admin.sync()
    store.agents.failing = False

    assert await reader.admin.sync() == 1
    assert reader.served() == [("a1", "Agente a1")]


def _agent(agent_id: str) -> AgentConfig:
    return AgentConfig(
        id=agent_id, nome=f"Agente {agent_id}", factory_ia_model="ollama", model="m", descricao="d", prompt="p"
    )
