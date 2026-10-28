"""FA-05: o laço do poll da revisão da config e a leitura da revisão no startup (``config_revision_poll.py``).

Caso de uso real (``fake_config_admin``: repositórios em memória, runtime falso) com o diário do container sobre o
Mongo falso; a falha do Mongo é injetada na leitura da revisão.
"""

from __future__ import annotations

import asyncio

from src.application.use_cases.config_admin_use_case import ConfigAdmin
from src.infrastructure.repositories.mongo_config_audit_log import MongoConfigAuditLog
from src.infrastructure.web.config_revision_poll import poll_config_revision, track_config_revision
from tests.fakes.admin_config_app import agent, agent_body, fake_config_admin
from tests.fakes.logger import RecordingLogger
from tests.fakes.mongo import FakeMotorClient
from tests.fakes.repositories import (
    InMemoryAgentConfigRepository,
    InMemoryTeamConfigRepository,
    InMemoryToolRepository,
)
from tests.fakes.runtime import FakeAgentRuntime


class _FlakyRevision(MongoConfigAuditLog):
    """``revision`` falha como o Mongo fora enquanto ``failures`` > 0; ``reads`` conta as leituras."""

    def __init__(self, failures: int) -> None:
        super().__init__(FakeMotorClient()["agno"])
        self.failures = failures
        self.reads = 0
        self.read = asyncio.Event()

    async def revision(self) -> int:
        self.reads += 1
        self.read.set()
        if self.failures > 0:
            self.failures -= 1
            raise ConnectionError("mongo fora")
        return await super().revision()


def _replica(audit: MongoConfigAuditLog, agents: InMemoryAgentConfigRepository) -> tuple[ConfigAdmin, FakeAgentRuntime]:
    """Uma réplica: o caso de uso sobre ``agents`` e ``audit`` (compartilhados = o mesmo Mongo), runtime próprio."""
    runtime = FakeAgentRuntime()
    admin = fake_config_admin(
        agents, InMemoryTeamConfigRepository(), InMemoryToolRepository(), RecordingLogger(), runtime, audit
    )
    return admin, runtime


async def _until_reads(audit: _FlakyRevision, reads: int) -> None:
    while audit.reads < reads:
        audit.read.clear()
        await asyncio.wait_for(audit.read.wait(), timeout=5)


async def test_falhas_seguidas_logam_uma_vez_so_pelo_tipo_e_a_volta_que_se_recupera_troca_e_avisa():
    audit = _FlakyRevision(failures=3)
    admin, runtime = _replica(audit, InMemoryAgentConfigRepository([]))
    logger = RecordingLogger()

    poll = asyncio.create_task(poll_config_revision(admin, 0.001, logger))
    await _until_reads(audit, 5)
    poll.cancel()
    await asyncio.wait({poll})

    assert poll.cancelled()
    assert [(r.level, r.message, r.context) for r in logger.records][:3] == [
        (
            "warning",
            "Falha ao seguir a revisão da config; o runtime anterior segue servindo",
            {"error_type": "ConnectionError"},
        ),
        ("info", "Revisão da config voltou a ser seguida", {}),
        ("info", "Runtime trocado pela revisão nova da config", {"runtime_revision": 1}),
    ]
    assert runtime.revision == 1  # sem revisão do startup, a primeira volta que lê relê o store uma vez


async def test_escrita_de_outra_replica_e_servida_pelo_laco():
    audit = MongoConfigAuditLog(FakeMotorClient()["agno"])
    store_agents = InMemoryAgentConfigRepository([])
    other, _ = _replica(audit, store_agents)
    reader, runtime = _replica(audit, store_agents)
    await reader.track()

    poll = asyncio.create_task(poll_config_revision(reader, 0.001, RecordingLogger()))
    await other.agents.create(agent_body("a1"), actor="admin")
    for _ in range(500):
        if runtime.revision:
            break
        await asyncio.sleep(0.001)
    poll.cancel()
    await asyncio.wait({poll})

    served, _ = runtime.served
    assert [a.id for a in served] == ["a1"]  # type: ignore[attr-defined]


async def test_revisao_nao_lida_no_startup_loga_o_tipo_e_nao_impede_a_carga():
    audit = _FlakyRevision(failures=1)
    admin, runtime = _replica(audit, InMemoryAgentConfigRepository([agent("a1")]))
    logger = RecordingLogger()

    await track_config_revision(admin, logger)

    assert [(r.level, r.message, r.context) for r in logger.records] == [
        (
            "warning",
            "Revisão da config não lida no startup; o primeiro poll relê o store",
            {"error_type": "ConnectionError"},
        )
    ]
    assert await admin.sync() == 1 and runtime.revision == 1

