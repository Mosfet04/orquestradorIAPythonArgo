"""Entidades que o runtime serve agora (FA-04): holder mutável lido a cada request pelas rotas próprias.

As rotas de AG-UI e de cancel (``agui_router``, ``run_cancellation``) são montadas uma vez, no startup; a troca
a quente (``AgnoRuntime.swap``) só substitui o conteúdo daqui. ``replace`` é síncrono: o request vê o conjunto
anterior ou o novo, nunca a mistura, e o que já pegou a entidade (run em andamento) segue com ela.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence

from agno.agent import Agent
from agno.team import Team

from src.domain.ports import ILogger

Entity = Agent | Team


class ServedEntities:
    """Agentes e teams servidos e o registro do AG-UI (``by_id``), trocados juntos por ``replace``."""

    def __init__(self, logger: ILogger) -> None:
        self._logger = logger
        self.agents: tuple[Agent, ...] = ()
        self.teams: tuple[Team, ...] = ()
        self.by_id: Mapping[str, Entity] = {}
        """Id -> entidade do AG-UI: agentes antes de teams; id repetido fica com a primeira (``POST /agui``)."""

    def replace(self, agents: Sequence[Agent], teams: Sequence[Team]) -> None:
        """Passa a servir ``agents`` e ``teams``, numa atribuição só."""
        by_id = _register(agents, teams, self._logger)
        self.agents, self.teams, self.by_id = tuple(agents), tuple(teams), by_id
        if by_id:
            self._logger.info(
                "AG-UI servido por entidade; POST /agui é alias deprecated da primeira",
                entity_ids=list(by_id),
                default_entity_id=next(iter(by_id)),
            )


def _register(agents: Sequence[Agent], teams: Sequence[Team], logger: ILogger) -> dict[str, Entity]:
    """Registro id -> entidade, agentes antes de teams; id repetido fica com o primeiro."""
    entities: dict[str, Entity] = {}
    candidates: list[Entity] = [*agents, *teams]
    for entity in candidates:
        entity_id = entity.id
        if not isinstance(entity_id, str) or not entity_id:
            logger.warning("Entidade sem id não exposta no AG-UI", entity_name=str(entity.name))
            continue
        if entity_id in entities:
            logger.warning("Id de entidade repetido; AG-UI expõe só a primeira", entity_id=entity_id)
            continue
        entities[entity_id] = entity
    return entities
