"""Serviço de criação de Teams — agno v2.5."""

from __future__ import annotations

from typing import Any, Dict, List, Sequence, Union

from agno.agent import Agent
from agno.db.mongo import MongoDb as MongoAgentDb
from agno.team import Team
from agno.team.mode import TeamMode

from src.application.services.agent_factory_service import (
    UserIdRequiredGuardrail,
    needs_user_id,
    requires_user_id,
)
from src.domain.entities.team_config import TeamConfig
from src.domain.ports import ILogger, IModelFactory

_MODE_MAP: Dict[str, TeamMode] = {
    "route": TeamMode.route,
    "coordinate": TeamMode.coordinate,
    "broadcast": TeamMode.broadcast,
    "tasks": TeamMode.tasks,
}


class TeamFactoryService:
    """Cria instâncias de ``Team`` (agno v2.5) a partir de ``TeamConfig``."""

    def __init__(
        self,
        *,
        db_url: str,
        db_name: str = "agno",
        logger: ILogger,
        model_factory: IModelFactory,
    ) -> None:
        self._db_url = db_url
        self._db_name = db_name
        self._logger = logger
        self._model_factory = model_factory

    def create_team(
        self,
        config: TeamConfig,
        agents: List[Agent],
    ) -> Team:
        """Cria um Team usando os agentes fornecidos como membros (síncrono, com I/O)."""
        members: Sequence[Union[Agent, Team]] = self._resolve_members(config, agents)
        # Síncrono e com I/O (segredo file:, DNS do destino): quem chama do caminho async
        # roda create_team via asyncio.to_thread (GetActiveTeamsUseCase).
        model: Any = self._model_factory.create_model(config.model_config)
        mode = _MODE_MAP.get(config.mode, TeamMode.route)
        db = MongoAgentDb(db_url=self._db_url, db_name=self._db_name)
        # O team repassa o user_id da requisição aos membros: exige se ele ou um membro
        # guarda memória de usuário (recusa logo no team, antes de delegar).
        user_memories = agentic_memory = config.user_memory_active
        guarded = needs_user_id(
            user_memories=user_memories, agentic_memory=agentic_memory
        ) or any(requires_user_id(member) for member in members)

        team = Team(
            id=config.id,
            name=config.nome,
            mode=mode,
            model=model,
            members=list(members),
            description=config.descricao or "",
            instructions=config.prompt or None,
            db=db,
            pre_hooks=[UserIdRequiredGuardrail(config.id)] if guarded else None,
            markdown=True,
            respond_directly=(mode == TeamMode.route),
            enable_agentic_memory=agentic_memory,
            enable_user_memories=user_memories,
            enable_session_summaries=config.summary_active,
            add_history_to_context=True,
            num_team_history_runs=5,
            # ── persistência de mensagens ──
            store_history_messages=True,
            store_tool_messages=True,
            store_events=True,
            store_member_responses=True,
            # Sem envio de telemetria à Agno (AGNO_TELEMETRY explícito ainda prevalece).
            telemetry=False,
        )
        self._logger.info(
            "Team criado",
            team_id=config.id,
            mode=config.mode,
            member_count=len(members),
        )
        return team

    def _resolve_members(
        self,
        config: TeamConfig,
        agents: List[Agent],
    ) -> List[Agent]:
        """Filtra e valida agentes membros do team."""
        agent_map = {a.id: a for a in agents}
        members: List[Agent] = []
        for mid in config.member_ids:
            agent = agent_map.get(mid)
            if agent:
                members.append(agent)
            else:
                self._logger.warning(
                    "Membro não encontrado entre agentes ativos",
                    team_id=config.id,
                    member_id=mid,
                )
        if not members:
            raise ValueError(
                f"Team {config.id!r}: nenhum membro válido encontrado"
            )
        return members
