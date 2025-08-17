from typing import Optional, List, Union, Callable, Dict, Any, cast
import asyncio
from datetime import datetime, timedelta
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


class ModelCacheEntry:
    """Entrada de cache para modelos com TTL."""
    
    def __init__(self, model, ttl_minutes: int = 30):
        self.model = model
        self.created_at = datetime.utcnow()
        self.ttl = timedelta(minutes=ttl_minutes)
        self.hit_count = 0
    
    def is_expired(self) -> bool:
        return datetime.utcnow() > (self.created_at + self.ttl)
    
    def access(self):
        self.hit_count += 1
        return self.model


class AgentFactoryService:
    """Serviço responsável por criar instâncias de agentes com cache otimizado."""
    
    def __init__(self, 
                 db_url: str = "mongodb://localhost:27017", 
                 db_name: str = "agno",
                 tool_repository: Optional[IToolRepository] = None):
        self._db_url = db_url
        self._db_name = db_name
        self._tool_repository = tool_repository
        self._http_tool_factory = HttpToolFactory()
        self._model_factory = ModelFactory()
        self._embedder_model_factory = EmbedderModelFactory()
        
        # Cache para modelos com TTL
        self._model_cache: Dict[str, ModelCacheEntry] = {}
        self._cache_lock = asyncio.Lock()
    
    async def create_agent_async(self, config: AgentConfig) -> Agent:
        """Versão assíncrona para criação de agentes."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self.create_agent, config)
    
    def create_agent(self, config: AgentConfig) -> Agent:
        """Cria um agente baseado na configuração fornecida com cache otimizado."""
        start_time = datetime.utcnow()
        
        try:
            app_logger.debug("🤖 Criando agente", agent_id=config.id)
            
            # Validar configuração do modelo
            validation_result = self._model_factory.validate_model_config(
                config.factoryIaModel, 
                config.model
            )
            
            if not validation_result["valid"]:
                errors = "; ".join(validation_result["errors"])
                raise ValueError(f"Configuração de modelo inválida: {errors}")
            
            # Obter modelo com cache
            model = self._get_or_create_model_cached(config.factoryIaModel, config.model)
            
            # Criar componentes de forma otimizada
            memory_db = self._create_memory_db()
            memory = self._create_memory(memory_db, model)
            storage = self._create_storage()
            tools = self._create_tools(config.tools_ids) if config.tools_ids else []
            
            # Criar RAG se configurado
            knowledge_base = None
            if config.rag_config and config.rag_config.active:
                try:
                    knowledge_base = self._create_rag(config)
                except Exception as e:
                    app_logger.warning("⚠️ Erro ao criar RAG", error=str(e))
            
            # Criar agente
            agent = Agent(
                name=config.nome,
                model=model,
                description=config.descricao,
                instructions=config.prompt,
                tools=tools,
                storage=storage,
                memory=memory,
                show_tool_calls=True,
                debug_mode=False,
            )
            
            # TODO: Adicionar knowledge base quando suportado pela biblioteca agno
            
            creation_time = (datetime.utcnow() - start_time).total_seconds()
            app_logger.debug("✅ Agente criado com sucesso", 
                           agent_id=config.id, 
                           creation_time_seconds=round(creation_time, 3))
            
            return agent
            
        except Exception as e:
            creation_time = (datetime.utcnow() - start_time).total_seconds()
            app_logger.error("❌ Erro ao criar agente", 
                           agent_id=config.id, 
                           error=str(e),
                           creation_time_seconds=round(creation_time, 3))
            raise
    
    def _get_or_create_model_cached(self, factory_type: str, model_name: str):
        """Obtém modelo do cache ou cria novo com cache."""
        cache_key = f"{factory_type}_{model_name}"
        
        # Verificar cache
        if cache_key in self._model_cache:
            cache_entry = self._model_cache[cache_key]
            if not cache_entry.is_expired():
                app_logger.debug("🎯 Cache hit para modelo", 
                               factory_type=factory_type, 
                               model_name=model_name,
                               hit_count=cache_entry.hit_count)
                return cache_entry.access()
            else:
                # Cache expirado - remover
                del self._model_cache[cache_key]
        
        # Cache miss - criar novo modelo
        app_logger.debug("🔄 Criando novo modelo", 
                        factory_type=factory_type, 
                        model_name=model_name)
        
        model = self._model_factory.create_model(factory_type, model_name)
        
        # Adicionar ao cache
        self._model_cache[cache_key] = ModelCacheEntry(model)
        
        return model
        knowledge_base = None
        if config.rag_config and config.rag_config.active:
            if not config.rag_config.factoryIaModel or not config.rag_config.model:
                raise ValueError("Configuração de RAG deve ter factoryIaModel e model definidos")
            
            validate_rag = self._embedder_model_factory.validate_model_config(
                config.rag_config.factoryIaModel,
                config.rag_config.model
            )
            if not validate_rag["valid"]:
                errors = "; ".join(validate_rag["errors"])
                raise ValueError(f"Configuração de RAG inválida: {errors}")
            
            knowledge_base = self._create_rag(config)

        agent = Agent(
            name=config.nome,
            agent_id=config.id,
            model=model,  # Usar o modelo criado pelo factory
            reasoning=False,
            markdown=True,
            add_history_to_messages=True,
            description=config.descricao,
            add_datetime_to_instructions=True,
            storage=storage,
            user_id="ava",
            memory=memory,
            enable_agentic_memory=True,
            enable_user_memories=True,
            enable_session_summaries=True,
            instructions=config.prompt,
            num_history_responses=5,
            tools=tools,
            knowledge=knowledge_base,
            search_knowledge=True if knowledge_base else False,
            read_chat_history=True if knowledge_base else False,
        )
        
        return agent
    
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
                with open(doc_path, 'r', encoding='utf-8') as file:
                    content = file.read()
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

    def _create_memory_db(self) -> MongoMemoryDb:
        """Cria a instância do banco de dados de memória."""
        return MongoMemoryDb(
            collection_name="user_memories",
            db_url=self._db_url,
            db_name=self._db_name
        )
    
    def _create_memory(self, memory_db, model) -> Memory:
        """Cria a instância de memória do agente."""
        return Memory(
            db=memory_db,
            summarizer=SessionSummarizer(model=model),
        )
    
    def _create_storage(self) -> MongoDbStorage:
        """Cria a instância de armazenamento do agente."""
        return MongoDbStorage(
            collection_name="storage",
            db_url=self._db_url,
            db_name=self._db_name,
        )
