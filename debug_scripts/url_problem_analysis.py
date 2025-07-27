"""
Script simples para identificar o problema com substituição de parâmetros na URL.
"""

import os
import sys

# Adicionar o path raiz ao sys.path
current_dir = os.path.dirname(os.path.abspath(__file__))
parent_dir = os.path.dirname(current_dir)
sys.path.append(parent_dir)

print("🔍 PROBLEMA IDENTIFICADO: Substituição de Parâmetros na URL")
print("=" * 60)

print("\n📋 SITUAÇÃO ATUAL:")
print("No arquivo http_tool_factory_service.py, o código está assim:")

print("""
def http_function(**kwargs) -> str:
    # Para GET e DELETE, usar params na URL
    response = httpx.request(
        method=tool.http_method.value,
        url=tool.route,                    # ❌ URL não é processada
        params=kwargs,                     # ❌ Todos os parâmetros vão como query string
        headers=headers,
        timeout=30.0
    )
""")

print("\n❌ O PROBLEMA:")
print("1. A URL não está sendo processada para substituir placeholders como {id}")
print("2. TODOS os parâmetros estão sendo enviados como query parameters")
print("3. Para uma URL como '/usuarios/{id}', o {id} deveria ser substituído pelo valor")
print("4. Apenas parâmetros restantes deveriam ir como query parameters")

print("\n🔧 EXEMPLO DO PROBLEMA:")
print("URL configurada: https://api.exemplo.com/usuarios/{id}")
print("Parâmetros: {'id': 123, 'incluir_detalhes': True}")
print()
print("❌ O que acontece atualmente:")
print("  URL chamada: https://api.exemplo.com/usuarios/{id}?id=123&incluir_detalhes=True")
print("  Resultado: 404 ou erro porque {id} não foi substituído")
print()
print("✅ O que deveria acontecer:")
print("  URL chamada: https://api.exemplo.com/usuarios/123?incluir_detalhes=True")
print("  Resultado: Requisição correta")

print("\n💡 SOLUÇÃO:")
print("Modificar a função http_function para:")
print("1. Detectar placeholders na URL (ex: {id}, {user_id})")
print("2. Substituir placeholders pelos valores dos parâmetros")
print("3. Remover parâmetros usados na URL dos query parameters")
print("4. Usar apenas parâmetros restantes como query parameters")

print("\n🎯 CÓDIGO CORRIGIDO:")
print("""
def http_function(**kwargs) -> str:
    # 1. Processar URL para substituir placeholders
    url = tool.route
    remaining_params = kwargs.copy()
    
    # 2. Substituir placeholders na URL
    for key, value in kwargs.items():
        placeholder = f"{{{key}}}"
        if placeholder in url:
            url = url.replace(placeholder, str(value))
            remaining_params.pop(key)  # Remover da lista de query params
    
    # 3. Usar apenas parâmetros restantes como query parameters
    if tool.http_method in [HttpMethod.GET, HttpMethod.DELETE]:
        response = httpx.request(
            method=tool.http_method.value,
            url=url,                      # ✅ URL com placeholders substituídos
            params=remaining_params,      # ✅ Apenas parâmetros não usados na URL
            headers=headers,
            timeout=30.0
        )
""")

print("\n🚀 PRÓXIMOS PASSOS:")
print("1. Implementar a correção no arquivo http_tool_factory_service.py")
print("2. Testar com URLs que têm placeholders")
print("3. Testar com URLs sem placeholders")
print("4. Verificar se query parameters funcionam corretamente")
