"""
Script para debugar o ToolRepository e testar busca de tools.
Use para testar a conexão com MongoDB e busca de tools.
"""

import sys
import os

# Adicionar o diretório raiz ao path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.dependency_injection import DependencyContainer
from src.domain.entities.tool import Tool, ToolParameter, HttpMethod, ParameterType


def main():
    """Testa o repositório de tools."""
    print("🛠️ Iniciando debug do ToolRepository...")
    
    # Carregar configurações
    config = AppConfig.load()
    print(f"📋 Configuração carregada: {config.app_title}")
    print(f"🗄️ MongoDB: {config.database.connection_string}")
    print(f"📊 Database: {config.database.database_name}")
    
    # Criar container de dependências
    container = DependencyContainer(config)
    
    # Obter o repositório
    tool_repository = container.get_tool_repository()
    print("✅ ToolRepository criado com sucesso")
    
    # Testar busca de todas as tools ativas
    print("\n🔍 Buscando todas as tools ativas...")
    try:
        all_tools = tool_repository.get_all_active_tools()
        print(f"✅ Encontradas {len(all_tools)} tools ativas")
        
        if all_tools:
            print("📋 Lista de tools encontradas:")
            for i, tool in enumerate(all_tools):
                print(f"   {i+1}. ID: {tool.id}")
                print(f"      Nome: {tool.name}")
                print(f"      Método: {tool.http_method.value}")
                print(f"      Rota: {tool.route}")
                print(f"      Parâmetros: {len(tool.parameters)}")
                print()
        else:
            print("⚠️ Nenhuma tool encontrada na collection 'tools'")
            print("💡 Dica: Adicione algumas tools na collection para testar")
    except Exception as e:
        print(f"❌ Erro ao buscar tools: {e}")
        print("💡 Verifique se o MongoDB está rodando e a collection existe")
    
    # Testar busca por IDs específicos
    print("\n🎯 Testando busca por IDs específicos...")
    test_ids = ["get-weather", "get-user-info", "create-user"]
    
    try:
        tools_by_ids = tool_repository.get_tools_by_ids(test_ids)
        print(f"✅ Busca por IDs retornou {len(tools_by_ids)} tools")
        
        if tools_by_ids:
            print("📋 Tools encontradas por ID:")
            for tool in tools_by_ids:
                print(f"   • {tool.id}: {tool.name}")
        else:
            print("⚠️ Nenhuma tool encontrada com os IDs fornecidos")
    except Exception as e:
        print(f"❌ Erro ao buscar tools por ID: {e}")
    
    # Testar busca por ID individual (pode gerar erro se não existir)
    print("\n🔍 Testando busca por ID individual...")
    test_id = "get-weather"
    
    try:
        single_tool = tool_repository.get_tool_by_id(test_id)
        print(f"✅ Tool encontrada: {single_tool.name}")
        print(f"   Descrição: {single_tool.description}")
        print(f"   Método: {single_tool.http_method.value}")
        print(f"   Rota: {single_tool.route}")
        
        if single_tool.parameters:
            print(f"   Parâmetros ({len(single_tool.parameters)}):")
            for param in single_tool.parameters:
                required = " (obrigatório)" if param.required else " (opcional)"
                print(f"     - {param.name}: {param.type.value}{required}")
                print(f"       {param.description}")
        
        if single_tool.instructions:
            print(f"   Instruções: {single_tool.instructions}")
            
    except ValueError as e:
        print(f"⚠️ Tool não encontrada: {e}")
        print("💡 Isso é normal se a tool não existir no banco")
    except Exception as e:
        print(f"❌ Erro inesperado: {e}")
    
    print("\n🎯 Debug concluído!")
    print("\n💡 Dicas para usar o debug:")
    print("   - Coloque breakpoints nas linhas que quer investigar")
    print("   - Use F10 para step over, F11 para step into")
    print("   - Examine as variáveis na aba 'Variables' do VS Code")
    print("   - Use o console do debug para executar código Python")


if __name__ == "__main__":
    main()
