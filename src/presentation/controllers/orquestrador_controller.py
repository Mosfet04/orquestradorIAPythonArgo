from typing import List
from agno.agent import Agent
from agno.playground import Playground
from agno.app.fastapi import FastAPIApp
from src.application.use_cases.get_active_agents_use_case import GetActiveAgentsUseCase


class OrquestradorController:
    """Controller responsável por gerenciar o orquestrador de agentes."""
    
    def __init__(self, get_active_agents_use_case: GetActiveAgentsUseCase):
        self._get_active_agents_use_case = get_active_agents_use_case
        self._agents_cache = None
    
    def get_agents(self) -> List[Agent]:
        """Obtém a lista de agentes configurados."""
        if self._agents_cache is None:
            self._agents_cache = self._get_active_agents_use_case.execute()
        return self._agents_cache
    
    def create_playground(self) -> Playground:
        """Cria uma instância do playground."""
        agents = self.get_agents()
        return Playground(
            agents=agents,
            name="Playground",
            description="A playground for agents multiplos",
            app_id="playground",
        )
    
    def create_fastapi_app(self) -> FastAPIApp:
        """Cria uma instância da aplicação FastAPI."""
        agents = self.get_agents()
        return FastAPIApp(
            agents=agents,
            name="Api Fast",
            app_id="api_fast",
            description="Api Fast para consumo dos multiplos agentes",
        )
    
    def refresh_agents(self):
        """Força atualização da cache de agentes."""
        self._agents_cache = None
