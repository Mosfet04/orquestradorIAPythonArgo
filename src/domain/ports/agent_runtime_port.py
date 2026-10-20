"""Porta do runtime de agentes (F2-04): monta Agent/Team a partir da configuração e troca os servidos (FA-04).

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
    def id(self) -> str:
        """Id do agente (o mesmo da configuração)."""
        ...


class TeamHandle(Protocol):
    """Team montado pelo runtime; fora dele, só o ``id`` (o da configuração) é lido."""

    @property
    def id(self) -> str:
        """Id do team (o mesmo da configuração)."""
        ...


class AgentRuntime(ABC):
    """Monta uma entidade por vez e troca as entidades servidas (FA-04).

    A política de carga (id repetido, isolamento de falha, paralelismo e log) é dos casos de uso.
    """

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

    @property
    @abstractmethod
    def revision(self) -> int:
        """Revisão das entidades servidas: 0 = as do startup; cada ``swap`` soma 1."""

    @abstractmethod
    def swap(self, agents: Sequence[AgentHandle], teams: Sequence[TeamHandle]) -> int:
        """Passa a servir ``agents`` e ``teams`` (handles deste runtime) no lugar dos atuais; devolve a revisão nova.

        Atômica para quem lê: o request seguinte vê o conjunto novo inteiro; o run em andamento termina com a
        instância que já pegou. Síncrona e sem I/O (nada intercala no event loop); falha sobe antes de trocar.
        """
