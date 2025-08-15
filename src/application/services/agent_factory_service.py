from typing import Optional, List, Text
from agno.agent import Agent
from agno.models.ollama import Ollama
from agno.storage.mongodb import MongoDbStorage
from agno.memory.v2.memory import Memory
from agno.memory.v2.summarizer import SessionSummarizer
from agno.memory.v2.db.mongodb import MongoMemoryDb
from agno.tools import Toolkit
from agno.knowledge.text import TextKnowledgeBase
from agno.vectordb.mongodb import MongoDb
from agno.embedder.ollama import OllamaEmbedder
from src.domain.entities.agent_config import AgentConfig
from src.domain.repositories.tool_repository import IToolRepository
from src.application.services.http_tool_factory_service import HttpToolFactory
from src.application.services.model_factory_service import ModelFactory


class AgentFactoryService:
    """Serviço responsável por criar instâncias de agentes."""
    
    def __init__(self, 
                 db_url: str = "mongodb://localhost:27017", 
                 db_name: str = "agno",
                 tool_repository: Optional[IToolRepository] = None):
        self._db_url = db_url
        self._db_name = db_name
        self._tool_repository = tool_repository
        self._http_tool_factory = HttpToolFactory()
        self._model_factory = ModelFactory()
    
    def create_agent(self, config: AgentConfig) -> Agent:
        """Cria um agente baseado na configuração fornecida."""
        # Validar configuração do modelo
        validation_result = self._model_factory.validate_model_config(
            config.factoryIaModel, 
            config.model
        )
        
        if not validation_result["valid"]:
            errors = "; ".join(validation_result["errors"])
            raise ValueError(f"Configuração de modelo inválida: {errors}")
        
        # Criar modelo usando o factory
        model = self._model_factory.create_model(config.factoryIaModel, config.model)
        
        memory_db = self._create_memory_db()
        memory = self._create_memory(memory_db, model)
        storage = self._create_storage()
        tools = self._create_tools(config.tools_ids) if config.tools_ids else []
        knowledge_base = None
        if config.rag_config and config.rag_config.active:
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
    
    def _create_tools(self, tool_ids: List[str]) -> List[Toolkit]:
        """Cria as ferramentas para o agente."""
        if not self._tool_repository or not tool_ids:
            return []
        
        try:
            # Buscar configurações das tools no repositório
            tool_configs = self._tool_repository.get_tools_by_ids(tool_ids)
            
            # Criar tools do agno usando o factory
            agno_tools = self._http_tool_factory.create_tools_from_configs(tool_configs)
            
            return agno_tools
        except Exception as e:
            # Log do erro (em ambiente real, usar logging adequado)
            print(f"Erro ao criar tools: {e}")
            return []
    def _create_rag(self, config: AgentConfig) -> TextKnowledgeBase:
        """Cria a base de conhecimento Rag para o agente."""
        
        # Define o modelo do embedder, usando um padrão se não especificado
        embedder_model = "nomic-embed-text:latest"
        if config.rag_config and hasattr(config.rag_config, 'model') and config.rag_config.model:
            embedder_model = config.rag_config.model
            
        try:
            knowledge_base = TextKnowledgeBase(
                vector_db=MongoDb(
                    collection_name="rag",
                    db_url=self._db_url,
                    database=self._db_name,
                    embedder=OllamaEmbedder(id=embedder_model),
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
