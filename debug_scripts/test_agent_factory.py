"""
Script para debugar o AgentFactoryService e testar criação de agentes.
Use para testar a criação de agentes com e sem tools.
"""

import sys
import os

# Adicionar o diretório raiz ao path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.domain.entities.agent_config import AgentConfig
from src.infrastructure.config.app_config import AppConfig
from src.infrastructure.dependency_injection import DependencyContainer


def main():
    """Testa a criação de agentes com tools."""
    print("🔧 Iniciando debug do AgentFactoryService...")
    
    # Carregar configurações
    config = AppConfig.load()
    print(f"📋 Configuração carregada: {config.app_title}")
    
    # Criar container de dependências
    container = DependencyContainer(config)
    
    # Obter o service
    agent_factory = container.get_agent_factory_service()
    print("✅ AgentFactoryService criado com sucesso")
    
    # Criar configuração de agente sem tools
    agent_config_simple = AgentConfig(
        id="debug-agent-simple",
        nome="Agente Debug Simples",
        model="llama3.2:latest",
        descricao="Agente para debug sem tools",
        prompt="Você é um assistente de debug simples."
    )
    
    print("\n🤖 Criando agente simples (sem tools)...")
    try:
        agent_simple = agent_factory.create_agent(agent_config_simple)
        print(f"✅ Agente criado: {agent_simple.name}")
        print(f"   ID: {agent_simple.agent_id}")
        print(f"   Modelo: {agent_simple.model}")
        print(f"   Tools: {len(agent_simple.tools) if hasattr(agent_simple, 'tools') and agent_simple.tools else 0}")
    except Exception as e:
        print(f"❌ Erro ao criar agente simples: {e}")
    
    # Criar configuração de agente com tools (pode falhar se não houver tools no banco)
    agent_config_with_tools = AgentConfig(
        id="debug-agent-tools",
        nome="Agente Debug com Tools",
        model="llama3.2:latest",
        descricao="Agente para debug com tools",
        prompt="Você é um assistente que pode usar ferramentas HTTP.",
        tools_ids=["get-weather", "get-user-info"]  # Exemplo de IDs
    )
    
    print("\n🛠️ Criando agente com tools...")
    try:
        agent_with_tools = agent_factory.create_agent(agent_config_with_tools)
        print(f"✅ Agente criado: {agent_with_tools.name}")
        print(f"   ID: {agent_with_tools.agent_id}")
        print(f"   Modelo: {agent_with_tools.model}")
        print(f"   Tools: {len(agent_with_tools.tools) if hasattr(agent_with_tools, 'tools') and agent_with_tools.tools else 0}")
        
        if hasattr(agent_with_tools, 'tools') and agent_with_tools.tools:
            print("   Lista de tools:")
            for i, tool in enumerate(agent_with_tools.tools):
                print(f"     {i+1}. {tool.name if hasattr(tool, 'name') else str(tool)}")
    except Exception as e:
        print(f"⚠️ Erro ao criar agente com tools: {e}")
        print("   (Isso é normal se não houver tools configuradas no banco)")
    
    print("\n🎯 Debug concluído!")


if __name__ == "__main__":
    main()
