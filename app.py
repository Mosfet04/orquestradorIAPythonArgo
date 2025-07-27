from typing import List
from agno.agent import Agent
from agno.models.ollama import Ollama
from agno.playground import Playground
from agno.storage.mongodb import MongoDbStorage
from agno.memory.v2 import Memory
from agno.memory.v2.summarizer import SessionSummarizer
from agno.memory.v2.db.mongodb import MongoMemoryDb
from pymongo import MongoClient
from agno.app.fastapi import FastAPIApp
from fastapi import FastAPI

def busca_agentes_configurados() -> List[Agent]:
    client = MongoClient("mongodb://localhost:27017")
    db = client["agno"]
    collection = db["agents_config"]

    query = {"active": True}
    resultados = collection.find(query)
    agentes = []
    for agent in resultados:
        memory_db = MongoMemoryDb(
            collection_name="user_memories", 
            db_url="mongodb://localhost:27017", 
            db_name="agno"
        )
        memory = Memory(
            db=memory_db,
            summarizer=SessionSummarizer(model=Ollama(id="llama3.2:latest")),  # ✅ Modelo correto
        )
        agente_criado = Agent(
            name=agent.get("nome"),
            agent_id=agent.get("id"),
            model=Ollama(id=agent.get("model")),  

            reasoning=False, 
            
            markdown=True,
            add_history_to_messages=True,
            description=agent.get("descricao"),
            add_datetime_to_instructions=True,
            
            storage=MongoDbStorage(
                collection_name="storage",
                db_url="mongodb://localhost:27017",
                db_name="agno",
            ),
            
            user_id="ava",
            
            
            memory=memory, 
            enable_agentic_memory=True,  
            enable_user_memories=True,
            enable_session_summaries=True,

            instructions=agent.get("prompt"),
            knowledge=None,
            search_knowledge=False,
            num_history_responses=5,
        )
        agentes.append(agente_criado)
    return agentes

playground = Playground(
    agents=busca_agentes_configurados(),
    name="Playground",
    description="A playground for agents multiplos",
    app_id="playground",
)
fastapi_app = FastAPIApp(
    agents=busca_agentes_configurados(),
    name="Api Fast",
    app_id="api_fast",
    description="Api Fast para consumo dos multiplos agentes",
)

app = FastAPI(title="Orquestrador agno")
fast_app = fastapi_app.get_app()
playground_app = playground.get_app()
app.mount("/playground", playground_app)
app.mount("/api", fast_app)

if __name__ == "__main__":
    playground.serve(app="app:app", reload=True)