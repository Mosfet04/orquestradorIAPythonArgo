"""
Aplicação principal do Orquestrador de Agentes IA.
Implementa arquitetura Onion com Clean Code.
"""

from fastapi import FastAPI
from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.dependency_injection import DependencyContainer


def create_app() -> FastAPI:
    """Factory para criar a aplicação FastAPI."""
    # Carregar configurações
    config = AppConfig.load()
    
    # Configurar container de dependências
    container = DependencyContainer(config)
    
    # Obter controller
    orquestrador_controller = container.get_orquestrador_controller()
    
    # Criar aplicação principal
    app = FastAPI(title=config.app_title)
    
    # Criar sub-aplicações
    playground = orquestrador_controller.create_playground()
    fastapi_app = orquestrador_controller.create_fastapi_app()
    
    # Montar sub-aplicações
    playground_app = playground.get_app()
    fast_app = fastapi_app.get_app()
    
    app.mount("/playground", playground_app)
    app.mount("/api", fast_app)
    
    return app


# Criar instância da aplicação
app = create_app()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app:app", host="0.0.0.0", port=7777, reload=True)