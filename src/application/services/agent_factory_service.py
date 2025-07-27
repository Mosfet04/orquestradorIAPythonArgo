from agno.agent import Agent
from agno.models.ollama import Ollama
from agno.storage.mongodb import MongoDbStorage
from agno.memory.v2.memory import Memory
from agno.memory.v2.summarizer import SessionSummarizer
from agno.memory.v2.db.mongodb import MongoMemoryDb
from src.domain.entities.agent_config import AgentConfig


class AgentFactoryService:
    """Serviço responsável por criar instâncias de agentes."""
    
    def __init__(self, 
                 db_url: str = "mongodb://localhost:27017", 
                 db_name: str = "agno"):
        self._db_url = db_url
        self._db_name = db_name
    
    def create_agent(self, config: AgentConfig) -> Agent:
        """Cria um agente baseado na configuração fornecida."""
        memory_db = self._create_memory_db()
        memory = self._create_memory(memory_db)
        storage = self._create_storage()
        
        agent = Agent(
            name=config.nome,
            agent_id=config.id,
            model=Ollama(id=config.model),
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
            knowledge=None,
            search_knowledge=False,
            num_history_responses=5,
        )
        
        return agent
    
    def _create_memory_db(self) -> MongoMemoryDb:
        """Cria a instância do banco de dados de memória."""
        return MongoMemoryDb(
            collection_name="user_memories",
            db_url=self._db_url,
            db_name=self._db_name
        )
    
    def _create_memory(self, memory_db: MongoMemoryDb) -> Memory:
        """Cria a instância de memória do agente."""
        return Memory(
            db=memory_db,
            summarizer=SessionSummarizer(model=Ollama(id="llama3.2:latest")),
        )
    
    def _create_storage(self) -> MongoDbStorage:
        """Cria a instância de armazenamento do agente."""
        return MongoDbStorage(
            collection_name="storage",
            db_url=self._db_url,
            db_name=self._db_name,
        )
