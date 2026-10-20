"""Poll da revisão da config (FA-05): cada réplica passa a servir a escrita das outras, sem change streams.

O lifespan lê a revisão do store antes de carregar as entidades (``track_config_revision``) e, com o runtime
servindo, roda ``poll_config_revision`` numa task até o shutdown: a cada ``CONFIG_REVISION_POLL_SECONDS``,
``ConfigAdmin.sync`` compara a revisão do store com a servida e, se outra réplica escreveu, relê e troca. Sem
diário (store YAML) não há o que seguir: o lifespan não liga o poll e o reload é manual.

Falha (Mongo fora, montagem que falha) não derruba nada: o runtime anterior segue servindo e a volta seguinte
tenta de novo. Só a primeira falha de uma sequência loga (com o tipo, nunca o texto), e a volta que se recupera
avisa: Mongo fora por uma hora não vira 1.800 avisos.
"""

from __future__ import annotations

import asyncio

from src.application.use_cases.config_admin_use_case import ConfigAdmin
from src.domain.ports import ILogger


async def track_config_revision(admin: ConfigAdmin, logger: ILogger) -> None:
    """Revisão do store que a carga do startup vai servir; sem ela (Mongo fora), a primeira volta relê."""
    try:
        await admin.track()
    except Exception as exc:  # noqa: BLE001 - o app sobe sem o Mongo; o poll relê tudo; tipo no log
        logger.warning(
            "Revisão da config não lida no startup; o primeiro poll relê o store", error_type=type(exc).__name__
        )


async def poll_config_revision(admin: ConfigAdmin, interval_seconds: float, logger: ILogger) -> None:
    """Uma volta (``ConfigAdmin.sync``) a cada ``interval_seconds``, até a task ser cancelada."""
    failing = False
    while True:
        await asyncio.sleep(interval_seconds)
        try:
            runtime_revision = await admin.sync()
        except Exception as exc:  # noqa: BLE001 - o runtime anterior segue; a volta seguinte tenta; tipo no log
            if not failing:
                logger.warning(
                    "Falha ao seguir a revisão da config; o runtime anterior segue servindo",
                    error_type=type(exc).__name__,
                )
            failing = True
            continue
        if failing:
            logger.info("Revisão da config voltou a ser seguida")
        failing = False
        if runtime_revision is not None:
            logger.info("Runtime trocado pela revisão nova da config", runtime_revision=runtime_revision)
