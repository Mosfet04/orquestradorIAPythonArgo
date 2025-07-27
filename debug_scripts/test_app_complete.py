"""
Script para testar a aplicação completa e debugar problemas de inicialização.
Use para debugar issues na criação da aplicação FastAPI.
"""

import sys
import os

# Adicionar o diretório raiz ao path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import create_app
from src.infrastructure.config.app_config import AppConfig


def main():
    """Testa a criação da aplicação completa."""
    print("🚀 Iniciando debug da aplicação completa...")
    
    # Testar carregamento de configurações
    print("\n📋 Testando carregamento de configurações...")
    try:
        config = AppConfig.load()
        print(f"✅ Configuração carregada com sucesso")
        print(f"   Título: {config.app_title}")
        print(f"   MongoDB: {config.database.connection_string}")
        print(f"   Database: {config.database.database_name}")
    except Exception as e:
        print(f"❌ Erro ao carregar configurações: {e}")
        return
    
    # Testar criação da aplicação
    print("\n🏗️ Testando criação da aplicação...")
    try:
        app = create_app()
        print(f"✅ Aplicação criada com sucesso")
        print(f"   Título: {app.title}")
        print(f"   Versão: {getattr(app, 'version', 'N/A')}")
        
        # Listar rotas
        print("\n🛣️ Rotas disponíveis:")
        for route in app.routes:
            if hasattr(route, 'path'):
                methods = getattr(route, 'methods', ['N/A'])
                print(f"   {route.path} - {list(methods)}")
        
    except Exception as e:
        print(f"❌ Erro ao criar aplicação: {e}")
        import traceback
        traceback.print_exc()
        return
    
    # Testar endpoints básicos (se conseguir criar a app)
    print("\n🧪 Testando client de teste...")
    try:
        from fastapi.testclient import TestClient
        
        client = TestClient(app)
        
        # Testar rota raiz (se existir)
        try:
            response = client.get("/")
            print(f"✅ GET / - Status: {response.status_code}")
        except Exception as e:
            print(f"⚠️ GET / não disponível: {e}")
        
        # Testar rota de documentação
        try:
            response = client.get("/docs")
            print(f"✅ GET /docs - Status: {response.status_code}")
        except Exception as e:
            print(f"⚠️ GET /docs - Erro: {e}")
            
        # Testar playground
        try:
            response = client.get("/playground")
            print(f"✅ GET /playground - Status: {response.status_code}")
        except Exception as e:
            print(f"⚠️ GET /playground - Erro: {e}")
            
        # Testar API
        try:
            response = client.get("/api")
            print(f"✅ GET /api - Status: {response.status_code}")
        except Exception as e:
            print(f"⚠️ GET /api - Erro: {e}")
            
    except ImportError:
        print("⚠️ TestClient não disponível, pulando testes de endpoint")
    except Exception as e:
        print(f"❌ Erro nos testes de endpoint: {e}")
    
    print("\n🎯 Debug da aplicação concluído!")
    print("\n💡 Dicas:")
    print("   - Use breakpoints para parar em pontos específicos")
    print("   - Verifique a aba 'Problems' do VS Code para erros")
    print("   - Use o terminal integrado para ver logs completos")
    print("   - Configure variáveis de ambiente se necessário")


if __name__ == "__main__":
    main()
