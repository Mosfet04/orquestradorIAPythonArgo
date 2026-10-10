"""Porta do runtime de agentes (F2-04): monta Agent/Team a partir da configuração.

Fronteira do framework de agentes (hoje o Agno, em ``src/infrastructure/runtime/agno/``). Tem
uma implementação só, por exceção à regra de "porta só com 2+ implementações": sem ela,
``application`` e ``presentation`` importariam o framework para criar e tipar as entidades.

Os handles são opacos: o núcleo só guarda, conta e repassa; quem os consome (montagem do
AgentOS, rotas) é a infraestrutura do mesmo runtime que os criou.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from typing import Protocol

from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.team_config import TeamConfig


class AgentHandle(Protocol):
    """Agente montado pelo runtime; fora dele, só o ``id`` (o da configuração) é lido."""

    @property
    def id(self) -> str: ...


class TeamHandle(Protocol):
    """Team montado pelo runtime; fora dele, só o ``id`` (o da configuração) é lido."""

    @property
    def id(self) -> str: ...


class AgentRuntime(ABC):
    """Monta uma entidade por vez; a política de carga (id repetido, isolamento de falha,
    paralelismo e log) é dos casos de uso."""

    @abstractmethod
    async def build_agent(self, config: AgentConfig) -> AgentHandle:
        """Agente pronto para servir; não bloqueia o event loop.

        Falha sobe sem log (config de modelo recusada como ``InvalidModelConfigError``): quem
        chama loga uma vez, com o id. Partes opcionais que falham (tool, RAG) são logadas e
        o agente sobe sem elas.
        """

    @abstractmethod
    def build_team(self, config: TeamConfig, agents: Sequence[AgentHandle]) -> TeamHandle:
        """Team com os membros de ``config.member_ids`` achados em ``agents`` (handles deste runtime).

        Membro ausente de ``agents`` é ignorado; nenhum membro achado é falha (``ValueError``).
        Síncrono e com I/O bloqueante (segredo ``file:``, DNS do destino do modelo): no caminho
        async, chame via ``asyncio.to_thread``. Falha sobe sem log, como em ``build_agent``.
        """
