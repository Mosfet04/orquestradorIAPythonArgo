"""Serviço de criação de agentes — agno v2.5."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, List, Optional

from agno.agent import Agent
from agno.db.mongo import MongoDb as MongoAgentDb
from agno.knowledge import Knowledge
from agno.tools import Toolkit
from agno.vectordb.mongodb import MongoDb as MongoVectorDb

from src.domain.entities.agent_config import AgentConfig
from src.domain.ports import ILogger, IModelFactory, IEmbedderFactory, IToolFactory
from src.domain.repositories.tool_repository import IToolRepository


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
    ) -> None:
        self._db_url = db_url
        self._db_name = db_name
        self._logger = logger
        self._model_factory = model_factory
        self._embedder_factory = embedder_factory
        self._tool_factory = tool_factory
        self._tool_repository = tool_repository

    # ── public ──────────────────────────────────────────────────────

    async def create_agent(self, config: AgentConfig) -> Agent:
        """Cria um agente baseado na configuração fornecida."""
        start = datetime.now(timezone.utc)
        try:
            self._validate_model_config(config)
            model = self._model_factory.create_model(
                config.factory_ia_model, config.model
            )
            tools = await self._build_tools(config)
            knowledge = self._build_knowledge(config)
            db = self._build_db()
            agent = self._assemble_agent(config, model, db, tools, knowledge)
            elapsed = (datetime.now(timezone.utc) - start).total_seconds()
            self._logger.info(
                "Agente criado",
                agent_id=config.id,
                elapsed_s=round(elapsed, 3),
            )
            return agent
        except Exception as exc:
            elapsed = (datetime.now(timezone.utc) - start).total_seconds()
            self._logger.error(
                "Erro ao criar agente",
                agent_id=config.id,
                error=str(exc),
                elapsed_s=round(elapsed, 3),
            )
            raise

    # ── private ─────────────────────────────────────────────────────

    def _validate_model_config(self, config: AgentConfig) -> None:
        result = self._model_factory.validate_model_config(
            config.factory_ia_model, config.model
        )
        if not result["valid"]:
            errors = "; ".join(result["errors"])
            raise ValueError(f"Configuração de modelo inválida: {errors}")

    def _build_db(self) -> MongoAgentDb:
        """Cria instância unificada de db (storage + memory) — agno v2."""
        return MongoAgentDb(
            db_url=self._db_url,
            db_name=self._db_name,
        )

    async def _build_tools(self, config: AgentConfig) -> List[Any]:
        if not config.tools_ids:
            return []
        try:
            tool_configs = await self._tool_repository.get_tools_by_ids(
                config.tools_ids
            )
            return await self._tool_factory.create_tools_from_configs(tool_configs)
        except Exception as exc:
            self._logger.warning("Erro ao criar tools", error=str(exc))
            return []

    def _build_knowledge(self, config: AgentConfig) -> Optional[Knowledge]:
        rag = config.rag_config
        if not rag or not rag.active:
            return None

        if not rag.factory_ia_model or not rag.model:
            self._logger.warning(
                "RAG ativo sem factory_ia_model ou model — ignorando"
            )
            return None

        try:
            embedder = self._embedder_factory.create_model(
                rag.factory_ia_model, rag.model
            )
            knowledge = Knowledge(
                vector_db=MongoVectorDb(
                    collection_name="rag",
                    db_url=self._db_url,
                    database=self._db_name,
                    embedder=embedder,
                ),
            )
            self._load_document(knowledge, rag.doc_name)
            return knowledge
        except Exception as exc:
            self._logger.warning("Erro ao criar RAG", error=str(exc))
            return None

    def _load_document(
        self, knowledge: Knowledge, doc_name: Optional[str]
    ) -> None:
        if not doc_name:
            self._logger.info("Nenhum documento especificado para RAG")
            return
        doc_path = f"docs/{doc_name}"
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
                error=str(exc),
            )

    def _assemble_agent(
        self,
        config: AgentConfig,
        model: Any,
        db: MongoAgentDb,
        tools: List[Any],
        knowledge: Optional[Knowledge],
    ) -> Agent:
        return Agent(
            id=config.id,
            name=config.nome,
            model=model,
            db=db,
            user_id="ava",
            reasoning=False,
            markdown=True,
            description=config.descricao,
            instructions=config.prompt,
            add_history_to_context=True,
            add_datetime_to_context=True,
            num_history_runs=5,
            enable_agentic_memory=config.user_memory_active,
            enable_user_memories=config.user_memory_active,
            enable_session_summaries=config.summary_active,
            tools=tools or None,
            knowledge=knowledge,
            search_knowledge=bool(knowledge),
            read_chat_history=bool(knowledge),
        )
