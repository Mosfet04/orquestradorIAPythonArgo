"""Serviço de criação de agentes — agno v2.5."""

from __future__ import annotations

import asyncio
import hashlib
import re
import unicodedata
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, List, Optional

from agno.agent import Agent
from agno.db.mongo import MongoDb as MongoAgentDb
from agno.exceptions import CheckTrigger, InputCheckError
from agno.guardrails.base import BaseGuardrail
from agno.knowledge import Knowledge
from agno.run.agent import RunInput
from agno.run.team import TeamRunInput
from agno.team import Team
from agno.vectordb.mongodb import MongoDb as MongoVectorDb

from src.application.services.document_indexing_service import DocumentIndexingService
from src.application.services.document_path import (
    DocumentPathError,
    resolve_document_path,
)
from src.application.services.knowledge_search_factory import KnowledgeSearchFactory
from src.domain.entities.agent_config import AgentConfig
from src.domain.entities.rag_config import SearchStrategy
from src.domain.ports import (
    IEmbedderFactory,
    ILogger,
    IModelFactory,
    InvalidModelConfigError,
    IToolFactory,
)
from src.domain.repositories.tool_repository import IToolRepository
from src.infrastructure.tools.hierarchical_search_tool import (
    create_hierarchical_search_tool,
)

_SIMPLE_AGENT_ID = re.compile(r"[a-z0-9-]{1,64}")
_SLUG_MAX_LENGTH = 40
_HASH_LENGTH = 12


def rag_collection_name(agent_id: str) -> str:
    """Coleção vetorial do RAG semântico de um agente (F1-07, B6).

    Antes, todos os agentes usavam a coleção ``rag``: um agente buscava nos documentos dos
    outros e embedders de dimensões diferentes colidiam no mesmo índice vetorial.

    - id simples (``[a-z0-9-]{1,64}``): ``rag_<id>`` (ex.: ``rag_suporte-n1``);
    - qualquer outro id: ``rag_<slug>_<sha256(id)[:12]>``, com o slug só em ``[a-z0-9-]``.

    Nunca colide entre ids diferentes: o id simples não tem ``_`` e a forma com hash tem
    sempre o ``_`` antes do hash, então os dois conjuntos são disjuntos; dentro da forma
    com hash, a colisão exigiria o mesmo prefixo de 48 bits do SHA-256. O nome resultante
    é válido no MongoDB (sem ``$``, NUL, ``.`` nem prefixo ``system.``) e curto (≤ 68).
    """
    if _SIMPLE_AGENT_ID.fullmatch(agent_id):
        return f"rag_{agent_id}"
    slug = re.sub(r"[^a-z0-9-]+", "-", agent_id.lower()).strip("-")
    slug = slug[:_SLUG_MAX_LENGTH].strip("-") or "agente"
    digest = hashlib.sha256(agent_id.encode("utf-8", "surrogatepass")).hexdigest()
    return f"rag_{slug}_{digest[:_HASH_LENGTH]}"


def _failure(exc: BaseException) -> dict[str, str]:
    """Contexto de log de uma falha: só o tipo (texto de SDK pode ter segredo); a recusa da
    fábrica de modelo/embedder é texto nosso e vai também como ``reason``."""
    context = {"error_type": type(exc).__name__}
    if isinstance(exc, InvalidModelConfigError):
        context["reason"] = str(exc)
    return context


def _read_document(doc_name: str) -> str:
    """Lê ``docs/<doc_name>`` já confinado a ``docs/`` (síncrono: rode em thread)."""
    return resolve_document_path(doc_name).read_text(encoding="utf-8")


# Usuário que o agno 2.5.8 usa para memória sem ``user_id`` (``agno/agent/_messages.py``,
# ``agno/memory/manager.py``): pool compartilhado, nunca um usuário de requisição.
_RESERVED_USER_IDS = frozenset({"default"})


def _has_visible_char(value: str) -> bool:
    """Algum caractere fora de separador (Z*) e controle/formatação/não atribuído (C*).

    ``str.strip`` não remove U+200B, U+FEFF, U+2060 nem NUL: sem isto, um valor que parece
    vazio viraria um usuário "anônimo" compartilhado.
    """
    return any(unicodedata.category(char)[0] not in ("Z", "C") for char in value)


def is_valid_user_id(user_id: object) -> bool:
    """``user_id`` aceito para memória: string com caractere visível e não reservada.

    Total (nunca levanta): o AG-UI repassa ``forwardedProps.user_id`` sem validar o tipo. Sem
    normalização: ``" ana "`` e ``"ana"`` são usuários diferentes.
    """
    return (
        isinstance(user_id, str)
        and user_id not in _RESERVED_USER_IDS
        and _has_visible_char(user_id)
    )


class UserIdRequiredGuardrail(BaseGuardrail):
    """Recusa run sem ``user_id`` válido de Agent/Team que guarda memória de usuário.

    Sem ``user_id``, o agno 2.5.8 lê e grava a memória no usuário ``"default"``
    (``agno/agent/_messages.py``, ``agno/agent/_managers.py``, ``agno/memory/manager.py``),
    compartilhado por todo chamador anônimo. Guardrail roda antes de o run ler memória
    ou chamar o modelo e sempre de forma síncrona (``agno/agent/_hooks.py``), inclusive
    com hooks em background. O ``user_id`` chega pelo nome do parâmetro
    (``agno/utils/hooks.py``, ``filter_hook_args``). Exceção que não seja
    ``InputCheckError`` é engolida pelo agno e o run segue: a checagem é total.
    """

    def __init__(self, entity_id: str) -> None:
        self.entity_id = entity_id

    def check(
        self,
        run_input: RunInput | TeamRunInput,
        user_id: object = None,
    ) -> None:
        if not is_valid_user_id(user_id):
            raise InputCheckError(
                f"user_id obrigatório: '{self.entity_id}' guarda memória por usuário. "
                "Envie user_id como string não vazia, com caractere visível e diferente "
                "de 'default' (campo do form em /agents|/teams/{id}/runs; "
                "forwardedProps.user_id no AG-UI).",
                check_trigger=CheckTrigger.VALIDATION_FAILED,
            )

    async def async_check(
        self,
        run_input: RunInput | TeamRunInput,
        user_id: object = None,
    ) -> None:
        self.check(run_input, user_id)


def needs_user_id(*, user_memories: bool, agentic_memory: bool) -> bool:
    """Predicado único (Agent e Team): memória de usuário ligada exige ``user_id``."""
    return user_memories or agentic_memory


def requires_user_id(entity: Agent | Team) -> bool:
    """``True`` se o Agent/Team lê ou grava memória de usuário (precisa de ``user_id``)."""
    return needs_user_id(
        user_memories=getattr(entity, "enable_user_memories", None) is True,
        agentic_memory=getattr(entity, "enable_agentic_memory", None) is True,
    )


class AgentFactoryService:
    """Cria instâncias de ``Agent`` (agno v2.5) a partir de ``AgentConfig``."""

    def __init__(
        self,
        *,
        db_url: str,
        db_name: str = "agno",
        logger: ILogger,
        model_factory: IModelFactory,
        embedder_factory: IEmbedderFactory,
        tool_factory: IToolFactory,
        tool_repository: IToolRepository,
        indexing_service: Optional[DocumentIndexingService] = None,
        search_factory: Optional[KnowledgeSearchFactory] = None,
    ) -> None:
        self._db_url = db_url
        self._db_name = db_name
        self._logger = logger
        self._model_factory = model_factory
        self._embedder_factory = embedder_factory
        self._tool_factory = tool_factory
        self._tool_repository = tool_repository
        self._indexing_service = indexing_service
        self._search_factory = search_factory

    # ── public ──────────────────────────────────────────────────────

    async def create_agent(self, config: AgentConfig) -> Agent:
        """Cria um agente baseado na configuração fornecida.

        Falha sobe sem log aqui: quem chama loga uma vez, com id e tipo do erro
        (``GetActiveAgentsUseCase``); o texto de exceção de SDK pode trazer segredo.
        """
        start = datetime.now(timezone.utc)
        # Config recusada sobe como InvalidModelConfigError; a criação pode ler segredo
        # (file:) e resolver DNS do destino: fora do event loop.
        model = await asyncio.to_thread(self._model_factory.create_model, config.model_config)
        tools = await self._build_tools(config)
        # Knowledge() consulta o Mongo (exists/create) e o insert lê, embeda e grava:
        # tudo síncrono no agno; fora do event loop.
        knowledge = await asyncio.to_thread(self._build_knowledge, config)

        # ── Estratégia hierárquica ──
        hierarchical_tool = await self._build_hierarchical_tool(config)
        if hierarchical_tool:
            tools.append(hierarchical_tool)

        db = self._build_db()
        agent = self._assemble_agent(config, model, db, tools, knowledge)
        elapsed = (datetime.now(timezone.utc) - start).total_seconds()
        self._logger.info(
            "Agente criado",
            agent_id=config.id,
            elapsed_s=round(elapsed, 3),
        )
        return agent

    # ── private ─────────────────────────────────────────────────────

    def _build_db(self) -> MongoAgentDb:
        """Cria instância unificada de db (storage + memory) — agno v2."""
        return MongoAgentDb(
            db_url=self._db_url,
            db_name=self._db_name,
        )

    async def _build_tools(self, config: AgentConfig) -> List[Any]:
        """Tools do agente; cada tool referenciada que não entra gera log de erro com os ids."""
        if not config.tools_ids:
            return []
        try:
            tool_configs = await self._tool_repository.get_tools_by_ids(
                config.tools_ids
            )
        except Exception as exc:
            self._logger.error(
                "Erro ao buscar tools do agente; agente sobe sem elas",
                agent_id=config.id,
                tool_ids=list(config.tools_ids),
                error_type=type(exc).__name__,
            )
            return []

        found = {tool.id for tool in tool_configs}
        for tool_id in dict.fromkeys(config.tools_ids):
            if tool_id not in found:
                self._logger.error(
                    "Tool referenciada pelo agente não encontrada (ausente, inativa "
                    "ou inválida); ignorada",
                    agent_id=config.id,
                    tool_id=tool_id,
                )

        tools: List[Any] = []
        for tool_config in tool_configs:
            try:
                created = await self._tool_factory.create_tools_from_configs(
                    [tool_config]
                )
            except Exception as exc:
                self._logger.error(
                    "Erro ao criar tool do agente; agente sobe sem ela",
                    agent_id=config.id,
                    tool_id=tool_config.id,
                    error_type=type(exc).__name__,
                )
                continue
            if not created:
                self._logger.error(
                    "Tool referenciada pelo agente não pôde ser criada; ignorada",
                    agent_id=config.id,
                    tool_id=tool_config.id,
                )
            tools.extend(created)
        return tools

    def _log_rejected_doc_name(self, agent_id: str, exc: DocumentPathError) -> None:
        self._logger.error(
            "doc_name do RAG rejeitado; RAG do agente desativado",
            agent_id=agent_id,
            reason=str(exc),
        )

    def _build_knowledge(self, config: AgentConfig) -> Optional[Knowledge]:
        """Knowledge do RAG semântico. Síncrono e com I/O: chame via ``asyncio.to_thread``."""
        rag = config.rag_config
        if not rag or not rag.active:
            return None

        # Estratégia hierárquica não usa Knowledge do agno
        if rag.search_strategy == SearchStrategy.HIERARCHICAL:
            return None

        embedder_config = rag.model_config
        if embedder_config is None:
            self._logger.warning(
                "RAG ativo sem factory_ia_model ou model — ignorando"
            )
            return None

        doc_path: Optional[Path] = None
        if rag.doc_name:
            try:
                doc_path = resolve_document_path(rag.doc_name)
            except DocumentPathError as exc:
                self._log_rejected_doc_name(config.id, exc)
                return None

        try:
            # Objeto do runtime (agno) criado pela fábrica do mesmo runtime; já fora do loop.
            embedder: Any = self._embedder_factory.create_embedder(embedder_config)
            knowledge = Knowledge(
                vector_db=MongoVectorDb(
                    collection_name=rag_collection_name(config.id),
                    db_url=self._db_url,
                    database=self._db_name,
                    embedder=embedder,
                ),
            )
            self._load_document(knowledge, doc_path)
            return knowledge
        except Exception as exc:
            self._logger.warning("Erro ao criar RAG", agent_id=config.id, **_failure(exc))
            return None

    async def _build_hierarchical_tool(self, config: AgentConfig) -> Optional[Any]:
        """Cria tool de busca hierárquica se a estratégia for HIERARCHICAL."""
        rag = config.rag_config
        if not rag or not rag.active:
            return None
        if rag.search_strategy != SearchStrategy.HIERARCHICAL:
            return None
        if not self._indexing_service or not self._search_factory:
            self._logger.warning(
                "Indexing service ou search factory não disponíveis "
                "para estratégia HIERARCHICAL"
            )
            return None
        if not rag.doc_name:
            self._logger.warning("doc_name obrigatório para HIERARCHICAL")
            return None

        try:
            # Indexar documento (idempotente); leitura confinada a docs/, fora do loop
            try:
                content = await asyncio.to_thread(_read_document, rag.doc_name)
            except DocumentPathError as exc:
                self._log_rejected_doc_name(config.id, exc)
                return None
            except FileNotFoundError:
                self._logger.warning(
                    "Documento não encontrado", path=f"docs/{rag.doc_name}"
                )
                return None

            await self._indexing_service.index_document(
                rag.doc_name, content, rag
            )

            # Criar embedder e estratégia
            embedder = await asyncio.to_thread(
                self._embedder_factory.create_embedder, rag.embedder_model_config()
            )
            strategy = self._search_factory.create_strategy(
                rag, embedder=embedder
            )
            return create_hierarchical_search_tool(strategy)
        except Exception as exc:
            self._logger.warning(
                "Erro ao criar tool hierárquica", agent_id=config.id, **_failure(exc)
            )
            return None

    def _load_document(
        self, knowledge: Knowledge, resolved_path: Optional[Path]
    ) -> None:
        """Indexa o documento na coleção do agente (já confinado a ``docs/``).

        ``skip_if_exists`` é checado na coleção do próprio agente (``content_hash_exists``
        do vector db, agno 2.5.8): coleção nova (inclusive a migração da ``rag``
        compartilhada) reindexa o documento no primeiro startup.
        """
        if resolved_path is None:
            self._logger.info("Nenhum documento especificado para RAG")
            return
        doc_path = resolved_path.as_posix()
        try:
            knowledge.insert(path=doc_path, skip_if_exists=True)
            self._logger.info("Documento RAG inserido", path=doc_path)
        except FileNotFoundError:
            self._logger.warning(
                "Documento não encontrado", path=doc_path
            )
        except Exception as exc:
            self._logger.error(
                "Erro ao carregar documento RAG",
                path=doc_path,
                error_type=type(exc).__name__,
            )

    def _assemble_agent(
        self,
        config: AgentConfig,
        model: Any,
        db: MongoAgentDb,
        tools: List[Any],
        knowledge: Optional[Knowledge],
    ) -> Agent:
        user_memories = agentic_memory = config.user_memory_active
        return Agent(
            id=config.id,
            name=config.nome,
            model=model,
            db=db,
            # Sem user_id fixo: vem da requisição; memória de usuário sem ele é recusada.
            pre_hooks=(
                [UserIdRequiredGuardrail(config.id)]
                if needs_user_id(
                    user_memories=user_memories, agentic_memory=agentic_memory
                )
                else None
            ),
            reasoning=False,
            markdown=True,
            description=config.descricao,
            instructions=config.prompt,
            add_history_to_context=True,
            add_datetime_to_context=True,
            num_history_runs=5,
            enable_agentic_memory=agentic_memory,
            enable_user_memories=user_memories,
            enable_session_summaries=config.summary_active,
            # ── persistência de mensagens ──
            store_history_messages=True,
            store_tool_messages=True,
            store_events=True,
            tools=tools or None,
            knowledge=knowledge,
            search_knowledge=bool(knowledge),
            read_chat_history=bool(knowledge),
            # Sem envio de telemetria à Agno (AGNO_TELEMETRY explícito ainda prevalece).
            telemetry=False,
        )
