import asyncio
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.dependency_injection import DependencyContainer
from src.infrastructure.logging import setup_structlog, app_logger
from typing import Optional

# Configurar logging estruturado
setup_structlog()

# Cache global para container (singleton pattern otimizado)
_container_cache: Optional[DependencyContainer] = None


async def configure_cors_async(app: FastAPI) -> None:
    """Configura CORS para uma aplicação FastAPI de forma assíncrona."""
    # Configuração CORS que realmente funciona com DevTunnels
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],               # Permitir todas as origens
        allow_credentials=False,           # IMPORTANTE: False quando usando allow_origins=["*"]
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS", "HEAD", "PATCH"],
        allow_headers=["*"],               # Todos os headers
        expose_headers=["*"],              # Expor todos os headers
    )
    
    # Middleware adicional para garantir CORS em DevTunnels
    @app.middleware("http")
    async def cors_handler(request, call_next):
        # Capturar a origem da requisição
        origin = request.headers.get("origin")
        
        # Processar a requisição
        if request.method == "OPTIONS":
            # Resposta para preflight requests
            from fastapi import Response
            response = Response()
            response.headers["Access-Control-Allow-Origin"] = origin or "*"
            response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS, HEAD, PATCH"
            response.headers["Access-Control-Allow-Headers"] = "*"
            response.headers["Access-Control-Max-Age"] = "86400"
            return response
        
        # Processar requisição normal
        response = await call_next(request)
        
        # Adicionar headers CORS explicitamente
        if origin:
            response.headers["Access-Control-Allow-Origin"] = origin
        else:
            response.headers["Access-Control-Allow-Origin"] = "*"
        
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS, HEAD, PATCH"
        response.headers["Access-Control-Allow-Headers"] = "*"
        response.headers["Access-Control-Expose-Headers"] = "*"
        
        return response


async def apply_additional_cors(app: FastAPI) -> None:
    """Aplica CORS adicional para garantir compatibilidade com DevTunnels."""
    
    @app.middleware("http")
    async def additional_cors_handler(request, call_next):
        # Headers CORS mais permissivos para DevTunnels
        origin = request.headers.get("origin", "*")
        
        # Resposta para OPTIONS (preflight)
        if request.method == "OPTIONS":
            from fastapi import Response
            response = Response(status_code=200)
            response.headers.update({
                "Access-Control-Allow-Origin": origin,
                "Access-Control-Allow-Methods": "*",
                "Access-Control-Allow-Headers": "*",
                "Access-Control-Allow-Credentials": "false",
                "Access-Control-Max-Age": "86400",
                "Vary": "Origin"
            })
            return response
        
        # Processar requisição normal
        response = await call_next(request)
        
        # Adicionar headers CORS à resposta
        response.headers.update({
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Methods": "*", 
            "Access-Control-Allow-Headers": "*",
            "Access-Control-Expose-Headers": "*",
            "Vary": "Origin"
        })
        
        return response


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Gerencia o ciclo de vida da aplicação com setup/cleanup assíncrono."""
    global _container_cache
    
    # Startup
    app_logger.info("🚀 Iniciando aplicação com otimizações de performance")
    try:
        # Carregar configurações assincronamente
        config = await AppConfig.load_async()
        
        # Inicializar container com pool de conexões otimizado
        _container_cache = await DependencyContainer.create_async(config)
        
        # Pre-aquecer cache de agentes
        orquestrador_controller = await _container_cache.get_orquestrador_controller_async()
        await orquestrador_controller.warm_up_cache()
        
        app_logger.info("✅ Aplicação inicializada com sucesso")
        
    except Exception as e:
        app_logger.critical("❌ Erro crítico na inicialização", 
                           error_type=e.__class__.__name__, error=str(e))
        raise
    
    yield
    
    # Shutdown
    app_logger.info("🔄 Finalizando aplicação")
    if _container_cache:
        await _container_cache.cleanup()
    app_logger.info("✅ Aplicação finalizada com sucesso")


async def create_playground_app_async(playground) -> FastAPI:
    """Extrai a aplicação FastAPI do playground de forma assíncrona."""
    # Executar operação potencialmente bloqueante em thread separada
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: playground.get_app())


async def create_fastapi_app_async(fastapi_app) -> FastAPI:
    """Extrai a aplicação FastAPI do FastAPIApp de forma assíncrona."""
    # Executar operação potencialmente bloqueante em thread separada
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, lambda: fastapi_app.get_app())


async def create_app_async() -> FastAPI:
    """Factory assíncrona para criar a aplicação FastAPI."""
    
    try:
        # Usar container do cache global
        global _container_cache
        if not _container_cache:
            config = await AppConfig.load_async()
            _container_cache = await DependencyContainer.create_async(config)
        
        # Obter controller assincronamente
        orquestrador_controller = await _container_cache.get_orquestrador_controller_async()
        
        # Criar aplicação principal com lifespan
        app = FastAPI(
            title=_container_cache.config.app_title,
            description="Orquestrador de Agentes IA com arquitetura otimizada",
            version="2.0.0",
            lifespan=lifespan
        )
        
        # Configurar CORS
        await configure_cors_async(app)
        
        # Criar sub-aplicações em paralelo para melhor performance
        app_logger.info("🔧 Criando sub-aplicações em paralelo")
        
        playground_task = asyncio.create_task(
            orquestrador_controller.create_playground_async()
        )
        fastapi_task = asyncio.create_task(
            orquestrador_controller.create_fastapi_app_async()
        )
        
        # Aguardar criação paralela
        playground, fastapi_app = await asyncio.gather(playground_task, fastapi_task)
        
        # Extrair apps FastAPI e configurar CORS em paralelo
        app_logger.info("⚡ Extraindo aplicações FastAPI")
        
        playground_app_task = asyncio.create_task(
            create_playground_app_async(playground)
        )
        fast_app_task = asyncio.create_task(
            create_fastapi_app_async(fastapi_app)
        )
        
        # Aguardar extração paralela
        playground_app, fast_app = await asyncio.gather(
            playground_app_task, 
            fast_app_task
        )
        
        # Configurar CORS em paralelo ANTES de configurar as próprias aplicações
        await asyncio.gather(
            configure_cors_async(playground_app),
            configure_cors_async(fast_app)
        )
        
        # Aplicar CORS adicional nas sub-aplicações
        await apply_additional_cors(playground_app)
        await apply_additional_cors(fast_app)
        
        # Montar sub-aplicações
        app.mount("/playground", playground_app)
        app.mount("/api", fast_app)
        
        app_logger.info("✅ Sub-aplicações montadas com sucesso")
        
        # Adicionar health check otimizado
        @app.get("/health")
        async def health_check():
            """Health check assíncrono com verificações de performance."""
            if _container_cache and _container_cache.health_service:
                return await _container_cache.health_service.check_async()
            return {"status": "healthy", "message": "Service running"}
        
        # Adicionar endpoint de métricas de cache
        @app.get("/metrics/cache")
        async def cache_metrics():
            """Endpoint para monitorar métricas de cache."""
            if _container_cache:
                controller = await _container_cache.get_orquestrador_controller_async()
                return controller.get_cache_stats()
            return {"status": "no_cache"}
        
        # Adicionar endpoint para refresh manual do cache
        @app.post("/admin/refresh-cache")
        async def refresh_cache():
            """Endpoint para forçar refresh do cache de agentes."""
            if _container_cache:
                controller = await _container_cache.get_orquestrador_controller_async()
                await controller.refresh_agents_async()
                return {"status": "cache_refreshed", "timestamp": asyncio.get_event_loop().time()}
            return {"status": "no_cache"}

        return app
        
    except Exception as e:
        app_logger.critical("❌ Erro crítico ao criar aplicação FastAPI", 
                           error_type=e.__class__.__name__, error=str(e))
        raise


def create_app() -> FastAPI:
    """Factory síncrona que evita conflitos de event loop."""
    import threading
    import queue
    
    # Usar thread separada para evitar conflito de event loop
    result_queue = queue.Queue()
    exception_queue = queue.Queue()
    
    def run_async():
        try:
            # Criar novo event loop para esta thread
            new_loop = asyncio.new_event_loop()
            asyncio.set_event_loop(new_loop)
            
            # Executar criação da app
            app_instance = new_loop.run_until_complete(create_app_async())
            result_queue.put(app_instance)
            
            new_loop.close()
        except Exception as e:
            exception_queue.put(e)
    
    # Executar em thread separada
    thread = threading.Thread(target=run_async)
    thread.start()
    thread.join()
    
    # Verificar se houve exceção
    if not exception_queue.empty():
        raise exception_queue.get()
    
    # Retornar resultado
    return result_queue.get()


# Criar instância da aplicação
app = create_app()


if __name__ == "__main__":
    import uvicorn

    # Configurações otimizadas do uvicorn para produção
    uvicorn_config = {
        "app": "app:app",
        "host": "0.0.0.0",
        "port": 7777,
        "reload": True,
        "workers": 1,  # Em desenvolvimento
        "access_log": False,  # Reduz I/O em dev
        "log_level": "info",
        # Configurações adicionais para performance
        "backlog": 2048,
        "h11_max_incomplete_event_size": 16384
    }
    
    # Adicionar uvloop apenas se disponível
    try:
        import uvloop
        uvicorn_config["loop"] = "uvloop"
        app_logger.info("🚀 Usando uvloop para melhor performance")
    except ImportError:
        app_logger.info("⚠️ uvloop não disponível, usando loop padrão")
    
    config = uvicorn.Config(**uvicorn_config)
    server = uvicorn.Server(config)
    asyncio.run(server.serve())