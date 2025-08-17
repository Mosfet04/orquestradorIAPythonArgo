"""
Factory para criação da aplicação FastAPI.

Esta classe centraliza toda a lógica de criação e configuração da aplicação,
mantendo o app.py limpo e seguindo os princípios da Onion Architecture.
"""
import asyncio
from contextlib import asynccontextmanager
from typing import Optional
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.dependency_injection import DependencyContainer
from src.infrastructure.logging import app_logger


class AppFactory:
    """Factory responsável por criar e configurar a aplicação FastAPI."""
    
    def __init__(self):
        self._container_cache: Optional[DependencyContainer] = None
    
    async def create_app_async(self) -> FastAPI:
        """Cria a aplicação FastAPI de forma assíncrona."""
        app_logger.info("🚀 Iniciando criação da aplicação FastAPI")
        
        try:
            # Criar aplicação com lifespan
            app = FastAPI(
                title="Orquestrador de Agentes IA",
                description="Sistema de orquestração de agentes IA com arquitetura otimizada",
                version="2.0.0",
                lifespan=self._lifespan
            )
            
            # Configurar CORS
            await self._configure_cors_async(app)
            
            # Obter container de dependências
            if not self._container_cache:
                config = await AppConfig.load_async()
                self._container_cache = await DependencyContainer.create_async(config)
            
            # Obter controller
            orquestrador_controller = await self._container_cache.get_orquestrador_controller_async()
            
            # Criar e montar sub-aplicações
            await self._mount_sub_applications(app, orquestrador_controller)
            
            # Adicionar endpoints de administração
            self._add_admin_endpoints(app)
            
            app_logger.info("✅ Aplicação FastAPI criada com sucesso")
            return app
            
        except Exception as e:
            app_logger.critical("❌ Erro crítico ao criar aplicação FastAPI", 
                              error_type=e.__class__.__name__, error=str(e))
            raise
    
    async def _configure_cors_async(self, app: FastAPI) -> None:
        """Configura CORS para a aplicação."""
        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_credentials=False,
            allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH"],
            allow_headers=["*"],
            expose_headers=["*"],
        )
        
        @app.middleware("http")
        async def cors_handler(request, call_next):
            origin = request.headers.get("origin")
            
            if request.method == "OPTIONS":
                from fastapi import Response
                response = Response()
                response.headers["Access-Control-Allow-Origin"] = origin or "*"
                response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS, HEAD, PATCH"
                response.headers["Access-Control-Allow-Headers"] = "*"
                response.headers["Access-Control-Max-Age"] = "86400"
                return response
            
            response = await call_next(request)
            response.headers["Access-Control-Allow-Origin"] = origin or "*"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS, HEAD, PATCH"
            response.headers["Access-Control-Allow-Headers"] = "*"
            response.headers["Access-Control-Expose-Headers"] = "*"
            
            return response
    
    async def _mount_sub_applications(self, app: FastAPI, controller) -> None:
        """Monta sub-aplicações na aplicação principal."""
        app_logger.info("🔧 Montando sub-aplicações")
        
        # Criar sub-aplicações em paralelo
        playground_task = asyncio.create_task(controller.create_playground_async())
        fastapi_task = asyncio.create_task(controller.create_fastapi_app_async())
        
        playground, fastapi_app = await asyncio.gather(playground_task, fastapi_task)
        
        # Extrair aplicações FastAPI
        playground_app_task = asyncio.create_task(
            self._extract_app_async(playground.get_app)
        )
        fast_app_task = asyncio.create_task(
            self._extract_app_async(fastapi_app.get_app)
        )
        
        playground_app, fast_app = await asyncio.gather(
            playground_app_task, fast_app_task
        )
        
        # Configurar CORS nas sub-aplicações
        await asyncio.gather(
            self._configure_cors_async(playground_app),
            self._configure_cors_async(fast_app)
        )
        
        # Montar sub-aplicações
        app.mount("/playground", playground_app)
        app.mount("/api", fast_app)
        
        app_logger.info("✅ Sub-aplicações montadas com sucesso")
    
    async def _extract_app_async(self, get_app_func) -> FastAPI:
        """Extrai aplicação FastAPI de forma assíncrona."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, get_app_func)
    
    def _add_admin_endpoints(self, app: FastAPI) -> None:
        """Adiciona endpoints administrativos."""
        @app.get("/health")
        async def health_check():
            """Health check assíncrono com verificações de performance."""
            if self._container_cache and self._container_cache.health_service:
                return await self._container_cache.health_service.check_async()
            return {"status": "healthy", "message": "Service running"}
        
        @app.get("/metrics/cache")
        async def cache_metrics():
            """Endpoint para monitorar métricas de cache."""
            if self._container_cache:
                controller = await self._container_cache.get_orquestrador_controller_async()
                return controller.get_cache_stats()
            return {"status": "no_cache"}
        
        @app.post("/admin/refresh-cache")
        async def refresh_cache():
            """Endpoint para forçar refresh do cache de agentes."""
            if self._container_cache:
                controller = await self._container_cache.get_orquestrador_controller_async()
                await controller.refresh_agents_async()
                return {"status": "cache_refreshed", "timestamp": asyncio.get_event_loop().time()}
            return {"status": "no_cache"}
    
    @asynccontextmanager
    async def _lifespan(self, app: FastAPI):
        """Gerencia o ciclo de vida da aplicação."""
        app_logger.info("🔄 Iniciando aplicação")
        
        # Startup
        try:
            if not self._container_cache:
                config = await AppConfig.load_async()
                self._container_cache = await DependencyContainer.create_async(config)
            
            # Aquecer cache de agentes
            controller = await self._container_cache.get_orquestrador_controller_async()
            await controller.warm_up_cache()
            
            app_logger.info("✅ Aplicação iniciada com sucesso")
            yield
            
        finally:
            # Shutdown
            app_logger.info("🔄 Finalizando aplicação")
            if self._container_cache:
                await self._container_cache.cleanup()
            app_logger.info("✅ Aplicação finalizada com sucesso")


# Instância global do factory
_app_factory = AppFactory()


async def create_app_async() -> FastAPI:
    """Factory assíncrona para criar a aplicação FastAPI."""
    return await _app_factory.create_app_async()


def create_app() -> FastAPI:
    """Factory síncrona que evita conflitos de event loop."""
    import threading
    import queue
    
    result_queue = queue.Queue()
    exception_queue = queue.Queue()
    
    def run_async():
        try:
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            app_instance = new_loop.run_until_complete(create_app_async())
            result_queue.put(app_instance)
            new_loop.close()
        except Exception as e:
            exception_queue.put(e)
    
    thread = threading.Thread(target=run_async)
    thread.start()
    thread.join()
    
    if not exception_queue.empty():
        raise exception_queue.get()
    
    return result_queue.get()
