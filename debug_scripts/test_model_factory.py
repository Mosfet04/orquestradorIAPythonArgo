#!/usr/bin/env python3
"""
Script de teste para demonstrar o funcionamento do ModelFactory.
Testa criação de agentes com diferentes tipos de modelos.
"""

import sys
import os

# Adicionar o diretório raiz ao path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.application.services.model_factory_service import ModelFactory
from src.application.services.agent_factory_service import AgentFactoryService
from src.domain.entities.agent_config import AgentConfig


def test_model_factory():
    """Testa o ModelFactory diretamente."""
    print("=== TESTE DO MODEL FACTORY ===\n")
    
    # Testar modelos suportados
    print("1. Modelos suportados:")
    supported_models = ModelFactory.get_supported_models()
    for model in supported_models:
        print(f"   - {model}")
    print()
    
    # Testar disponibilidade dos modelos
    print("2. Disponibilidade dos modelos:")
    available_models = ModelFactory.get_available_models()
    for model, available in available_models.items():
        status = "✅ Disponível" if available else "❌ Não disponível"
        print(f"   - {model}: {status}")
    print()
    
    # Testar criação de modelo Ollama
    print("3. Testando criação de modelo Ollama:")
    try:
        ollama_model = ModelFactory.create_model("ollama", "llama3.2:latest")
        print(f"   ✅ Modelo Ollama criado com sucesso: {type(ollama_model).__name__}")
        print(f"   📝 ID do modelo: {ollama_model.id}")
    except Exception as e:
        print(f"   ❌ Erro ao criar modelo Ollama: {e}")
    print()
    
    # Testar validação de configuração
    print("4. Testando validação de configuração:")
    
    # Configuração válida
    result = ModelFactory.validate_model_config("ollama", "llama3.2:latest")
    print(f"   Ollama + llama3.2:latest: {'✅ Válido' if result['valid'] else '❌ Inválido'}")
    if result['errors']:
        for error in result['errors']:
            print(f"     - Erro: {error}")
    
    # Configuração inválida
    result = ModelFactory.validate_model_config("invalid_model", "some-model")
    print(f"   invalid_model + some-model: {'✅ Válido' if result['valid'] else '❌ Inválido'}")
    if result['errors']:
        for error in result['errors']:
            print(f"     - Erro: {error}")
    print()


def test_agent_factory_with_different_models():
    """Testa o AgentFactoryService com diferentes tipos de modelos."""
    print("=== TESTE DO AGENT FACTORY COM DIFERENTES MODELOS ===\n")
    
    # Configurações de teste
    test_configs = [
        AgentConfig(
            id="agent-ollama-1",
            nome="Agente Ollama",
            factoryIaModel="ollama",
            model="llama3.2:latest",
            descricao="Agente usando modelo Ollama",
            prompt="Você é um assistente útil usando Ollama."
        ),
        AgentConfig(
            id="agent-openai-1",
            nome="Agente OpenAI",
            factoryIaModel="openai",
            model="gpt-3.5-turbo",
            descricao="Agente usando modelo OpenAI",
            prompt="Você é um assistente útil usando OpenAI."
        ),
        AgentConfig(
            id="agent-gemini-1",
            nome="Agente Gemini",
            factoryIaModel="gemini",
            model="gemini-pro",
            descricao="Agente usando modelo Gemini",
            prompt="Você é um assistente útil usando Gemini."
        )
    ]
    
    agent_factory = AgentFactoryService()
    
    for i, config in enumerate(test_configs, 1):
        print(f"{i}. Testando {config.factoryIaModel} ({config.model}):")
        
        try:
            agent = agent_factory.create_agent(config)
            
            print(f"   ✅ Agente criado com sucesso!")
            print(f"   📝 Nome: {agent.name}")
            print(f"   📝 ID: {agent.agent_id}")
            print(f"   📝 Modelo: {type(agent.model).__name__}")
            
        except Exception as e:
            print(f"   ❌ Erro ao criar agente: {e}")
        
        print()


def test_model_validation():
    """Testa validação específica de modelos."""
    print("=== TESTE DE VALIDAÇÃO DE MODELOS ===\n")
    
    test_cases = [
        ("ollama", "llama3.2:latest", True),
        ("OLLAMA", "llama3.2:latest", True),  # Case insensitive
        ("ollama", "", False),  # Model ID vazio
        ("", "llama3.2:latest", False),  # Factory type vazio
        ("invalid_model", "some-model", False),  # Modelo não suportado
        ("openai", "gpt-3.5-turbo", None),  # Pode estar disponível ou não
        ("gemini", "gemini-pro", None),  # Pode estar disponível ou não
    ]
    
    for i, (factory_type, model_id, expected) in enumerate(test_cases, 1):
        print(f"{i}. Validando '{factory_type}' + '{model_id}':")
        
        result = ModelFactory.validate_model_config(factory_type, model_id)
        
        if expected is None:
            # Para modelos que podem ou não estar disponíveis
            if result['supported']:
                status = "✅ Suportado" if result['available'] else "⚠️ Suportado mas não disponível"
            else:
                status = "❌ Não suportado"
        else:
            status = "✅ Válido" if result['valid'] == expected else "❌ Resultado inesperado"
        
        print(f"   {status}")
        print(f"   📝 Suportado: {result['supported']}")
        print(f"   📝 Disponível: {result['available']}")
        print(f"   📝 Válido: {result['valid']}")
        
        if result['errors']:
            print(f"   📝 Erros:")
            for error in result['errors']:
                print(f"     - {error}")
        
        print()


def main():
    """Função principal do script de teste."""
    print("🧪 SCRIPT DE TESTE - MODEL FACTORY E AGENT FACTORY")
    print("=" * 60)
    print()
    
    try:
        test_model_factory()
        test_agent_factory_with_different_models()
        test_model_validation()
        
        print("✅ Todos os testes foram executados!")
        
    except Exception as e:
        print(f"❌ Erro durante execução dos testes: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    main()
