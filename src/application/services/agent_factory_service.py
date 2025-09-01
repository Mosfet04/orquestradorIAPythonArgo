from typing import Optional, List, Union, Callable, Dict, Any, cast
import asyncio
from datetime import datetime
from agno.agent import Agent
from agno.storage.mongodb import MongoDbStorage
from agno.memory.v2.memory import Memory
from agno.memory.v2.summarizer import SessionSummarizer
from agno.memory.v2.db.mongodb import MongoMemoryDb
from agno.tools import Toolkit
from agno.tools.function import Function
from agno.knowledge.text import TextKnowledgeBase
from agno.vectordb.mongodb import MongoDb
from src.domain.entities.agent_config import AgentConfig
from src.domain.repositories.tool_repository import IToolRepository
from src.application.services.http_tool_factory_service import HttpToolFactory
from src.application.services.model_factory_service import ModelFactory
from src.application.services.embedder_model_factory_service import EmbedderModelFactory
from src.infrastructure.logging import app_logger
from src.infrastructure.cache.model_cache_service import ModelCacheService


class AgentFactoryService:
    """Serviço responsável por criar instâncias de agentes com cache otimizado."""
    
    def __init__(self, 
                 db_url: str = "mongodb://localhost:62659/?directConnection=true", 
                 db_name: str = "agno",
                 tool_repository: Optional[IToolRepository] = None):
        self._db_url = db_url
        self._db_name = db_name
        self._tool_repository = tool_repository
        self._http_tool_factory = HttpToolFactory()
        self._model_factory = ModelFactory()
        self._embedder_model_factory = EmbedderModelFactory()
        self._model_cache_service = ModelCacheService()
    
    async def create_agent_async(self, config: AgentConfig) -> Agent:
        """Versão assíncrona para criação de agentes."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.create_agent, config)
    
    def create_agent(self, config: AgentConfig) -> Agent:
        """Cria um agente baseado na configuração fornecida com cache otimizado."""
        start_time = datetime.utcnow()
        try:
            self._validate_model_config_or_raise(config)
            model = self._get_or_create_model_cached(config.factoryIaModel, config.model)
            memory_db = self._create_memory_db(config)
            memory = self._create_memory(config, memory_db, model)
            storage = self._create_storage()
            tools = self._get_tools_if_any(config)
            knowledge_base = self._get_knowledge_base_if_active(config)
            agent = self._build_agent(config, model, memory, storage, tools, knowledge_base)
            return agent
        except Exception as e:
            creation_time = (datetime.utcnow() - start_time).total_seconds()
            app_logger.error("❌ Erro ao criar agente", 
                             agent_id=config.id, 
                             error=str(e),
                             creation_time_seconds=round(creation_time, 3))
            raise

    def _validate_model_config_or_raise(self, config: AgentConfig):
        validation_result = self._model_factory.validate_model_config(
            config.factoryIaModel, 
            config.model
        )
        if not validation_result["valid"]:
            errors = "; ".join(validation_result["errors"])
            raise ValueError(f"Configuração de modelo inválida: {errors}")

    def _get_tools_if_any(self, config: AgentConfig):
        return self._create_tools(config.tools_ids) if config.tools_ids else []

    def _get_knowledge_base_if_active(self, config: AgentConfig):
        if config.rag_config and config.rag_config.active:
            try:
                return self._create_rag(config)
            except Exception as e:
                app_logger.warning("⚠️ Erro ao criar RAG", error=str(e))
        return None

    def _build_agent(self, config, model, memory, storage, tools, knowledge_base):
        return Agent(
            name=config.nome,
            agent_id=config.id,
            model=model,
            reasoning=False,
            markdown=True,
            add_history_to_messages=True,
            description=config.descricao,
            add_datetime_to_instructions=True,
            storage=storage,
            user_id="ava",
            memory=memory,
            enable_agentic_memory=True if config.user_memory_active else False,
            enable_user_memories=True if config.user_memory_active else False,
            enable_session_summaries=True if config.summary_active else False,
            instructions=config.prompt,
            num_history_responses=5,
            tools=tools,
            knowledge=knowledge_base,
            search_knowledge=True if knowledge_base else False,
            read_chat_history=True if knowledge_base else False,
        )
    
    async def _get_or_create_model_cached_async(self, factory_type: str, model_name: str):
        """Obtém modelo do cache ou cria novo de forma assíncrona."""
        cache_key = f"{factory_type}_{model_name}"
        
        return await self._model_cache_service.get_or_create(
            cache_key,
            self._model_factory.create_model,
            factory_type,
            model_name
        )
    
    def _get_or_create_model_cached(self, factory_type: str, model_name: str):
        """Obtém modelo do cache ou cria novo (versão síncrona)."""
        # Para manter compatibilidade, usar diretamente o factory sem cache
        # Em uma refatoração futura, todo o fluxo deve ser assíncrono
        return self._model_factory.create_model(factory_type, model_name)
    
    def _create_tools(self, tool_ids: List[str]) -> List[Union[Toolkit, Callable, Function, Dict[str, Any]]]:
        """Cria as ferramentas para o agente."""
        if not self._tool_repository or not tool_ids:
            return []
        
        try:
            # Buscar configurações das tools no repositório
            tool_configs = self._tool_repository.get_tools_by_ids(tool_ids)
            
            # Criar tools do agno usando o factory
            agno_tools = self._http_tool_factory.create_tools_from_configs(tool_configs)
            
            return cast(List[Union[Toolkit, Callable, Function, Dict[str, Any]]], agno_tools)
        except Exception as e:
            # Log do erro (em ambiente real, usar logging adequado)
            print(f"Erro ao criar tools: {e}")
            return []
    def _create_rag(self, config: AgentConfig) -> TextKnowledgeBase:
        """Cria a base de conhecimento Rag para o agente."""
        
        # Verificar se rag_config existe e tem os campos necessários
        if not config.rag_config:
            raise ValueError("Configuração de RAG não encontrada")
        
        if not config.rag_config.factoryIaModel or not config.rag_config.model:
            raise ValueError("Configuração de RAG deve ter factoryIaModel e model definidos")

        embedder_model = self._embedder_model_factory.create_model(
            config.rag_config.factoryIaModel, 
            config.rag_config.model
        )
            
        try:
            knowledge_base = TextKnowledgeBase(
                vector_db=MongoDb(
                    collection_name="rag",
                    db_url=self._db_url,
                    database=self._db_name,
                    embedder=embedder_model,
                )
            )
        except Exception as e:
            print(f"❌ Erro ao criar AgentKnowledge: {e}")
            raise
        
        # Carrega o arquivo de documentação se especificado
        if config.rag_config and hasattr(config.rag_config, 'doc_name') and config.rag_config.doc_name:
            doc_path = f"docs/{config.rag_config.doc_name}"
            try:
                with open(doc_path, 'r', encoding='utf-8'):
                    knowledge_base.load_document(path=doc_path)

            except FileNotFoundError:
                print(f"❌ Arquivo de documentação não encontrado: {doc_path}")
            except Exception as e:
                print(f"❌ Erro ao carregar documentação: {type(e).__name__}: {e}")
                import traceback
                traceback.print_exc()
        else:
            print("⚠️ Nenhum arquivo de documentação especificado para RAG")
            
        return knowledge_base

    def _create_memory_db(self, config: AgentConfig) -> Optional[MongoMemoryDb]:
        """Cria a instância do banco de dados de memória."""
        if config.user_memory_active:
            return MongoMemoryDb(
                collection_name="user_memories",
                db_url=self._db_url,
                db_name=self._db_name
            )
        return None
    
    def _create_memory(self, config: AgentConfig, memory_db, model) -> Optional[Memory]:
        """Cria a instância de memória do agente."""
        if config.user_memory_active:
            return Memory(
            db=memory_db,
            summarizer=SessionSummarizer(model=model),
        )
        return None
    
    def _create_storage(self) -> MongoDbStorage:
        """Cria a instância de armazenamento do agente."""
        return MongoDbStorage(
            collection_name="storage",
            db_url=self._db_url,
            db_name=self._db_name,
        )
