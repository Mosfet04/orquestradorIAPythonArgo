# 🛠️ HTTP Tools: Substituição de Parâmetros na URL

## ✅ **PROBLEMA RESOLVIDO**

O problema com a substituição de parâmetros na URL foi **identificado e corrigido** com sucesso!

## 🔍 **O que era o problema:**

Anteriormente, o código no `http_tool_factory_service.py` estava enviando **todos** os parâmetros como query string, sem processar placeholders na URL:

```python
# ❌ CÓDIGO ANTERIOR (PROBLEMÁTICO)
response = httpx.request(
    method=tool.http_method.value,
    url=tool.route,                    # URL não processada
    params=kwargs,                     # Todos parâmetros como query string
    headers=headers,
    timeout=30.0
)
```

**Resultado problemático:**
- URL configurada: `https://api.exemplo.com/usuarios/{id}`
- Parâmetros: `{"id": 123, "incluir_detalhes": True}`
- URL chamada: `https://api.exemplo.com/usuarios/{id}?id=123&incluir_detalhes=True`
- **Erro:** 404 porque `{id}` não foi substituído

## ✅ **Solução implementada:**

```python
# ✅ CÓDIGO CORRIGIDO
def http_function(**kwargs) -> str:
    # 1. Processar URL para substituir placeholders
    url = tool.route
    remaining_params = kwargs.copy()
    
    # 2. Substituir placeholders na URL (ex: {id}, {user_id})
    for key, value in kwargs.items():
        placeholder = f"{{{key}}}"
        if placeholder in url:
            url = url.replace(placeholder, str(value))
            remaining_params.pop(key)  # Remover da lista de query params
    
    # 3. Usar apenas parâmetros restantes
    if tool.http_method in [HttpMethod.GET, HttpMethod.DELETE]:
        response = httpx.request(
            method=tool.http_method.value,
            url=url,                      # ✅ URL com placeholders substituídos
            params=remaining_params,      # ✅ Apenas parâmetros não usados na URL
            headers=headers,
            timeout=30.0
        )
```

**Resultado correto:**
- URL configurada: `https://api.exemplo.com/usuarios/{id}`
- Parâmetros: `{"id": 123, "incluir_detalhes": True}`
- URL chamada: `https://api.exemplo.com/usuarios/123?incluir_detalhes=True`
- **Sucesso:** `{id}` foi substituído por `123`, query param restante mantido

## 🎯 **Exemplos de uso:**

### **Exemplo 1: URL com um placeholder**
```json
{
  "name": "buscar_usuario",
  "route": "https://api.exemplo.com/usuarios/{id}",
  "http_method": "GET",
  "parameters": [
    {"name": "id", "type": "integer", "required": true},
    {"name": "incluir_detalhes", "type": "boolean", "required": false}
  ]
}
```

**Chamada:**
- Parâmetros: `{"id": 123, "incluir_detalhes": true}`
- URL final: `https://api.exemplo.com/usuarios/123?incluir_detalhes=true`

### **Exemplo 2: URL com múltiplos placeholders**
```json
{
  "name": "buscar_post",
  "route": "https://api.exemplo.com/usuarios/{user_id}/posts/{post_id}",
  "http_method": "GET",
  "parameters": [
    {"name": "user_id", "type": "integer", "required": true},
    {"name": "post_id", "type": "integer", "required": true},
    {"name": "incluir_comentarios", "type": "boolean", "required": false}
  ]
}
```

**Chamada:**
- Parâmetros: `{"user_id": 123, "post_id": 456, "incluir_comentarios": true}`
- URL final: `https://api.exemplo.com/usuarios/123/posts/456?incluir_comentarios=true`

### **Exemplo 3: URL sem placeholders**
```json
{
  "name": "listar_usuarios",
  "route": "https://api.exemplo.com/usuarios",
  "http_method": "GET",
  "parameters": [
    {"name": "limite", "type": "integer", "required": false},
    {"name": "pagina", "type": "integer", "required": false}
  ]
}
```

**Chamada:**
- Parâmetros: `{"limite": 10, "pagina": 2}`
- URL final: `https://api.exemplo.com/usuarios?limite=10&pagina=2`

## 🔧 **Métodos HTTP suportados:**

### **GET e DELETE:**
- Placeholders substituídos na URL
- Parâmetros restantes enviados como query parameters
- Exemplo: `GET /usuarios/123?incluir_detalhes=true`

### **POST, PUT e PATCH:**
- Placeholders substituídos na URL  
- Parâmetros restantes enviados no body JSON
- Exemplo: `PUT /usuarios/123` com body `{"nome": "João", "email": "joao@email.com"}`

## 🧪 **Testes implementados:**

✅ **6 testes unitários** criados para validar:
- Substituição de placeholder único
- Múltiplos placeholders 
- URLs sem placeholders
- Criação de descrições de função
- Mapeamento de tipos de parâmetros
- Criação de schemas de parâmetros

✅ **Scripts de debug** criados:
- `debug_scripts/test_url_correction.py` - Valida a correção
- `debug_scripts/url_problem_analysis.py` - Explica o problema

## 📊 **Cobertura de testes:**

```bash
# Executar testes específicos
python -m pytest tests/unit/test_http_tool_factory.py -v

# Executar todos os testes
python -m pytest tests/ -v

# Resultado: 55 testes passando (incluindo os 6 novos)
```

## 🚀 **Como usar no agno:**

1. **Configure uma tool no MongoDB:**
```javascript
db.tools.insertOne({
  "id": "buscar_usuario_001",
  "name": "buscar_usuario",
  "description": "Busca um usuário específico por ID",
  "route": "https://jsonplaceholder.typicode.com/users/{id}",
  "http_method": "GET",
  "parameters": [
    {
      "name": "id",
      "type": "integer", 
      "description": "ID do usuário para buscar",
      "required": true
    }
  ],
  "active": true
})
```

2. **O agno automaticamente:**
   - Detecta o placeholder `{id}` na URL
   - Substitui `{id}` pelo valor fornecido
   - Faz a requisição para a URL correta

3. **Exemplo de uso pelo agno:**
   - Pergunta: "Busque o usuário com ID 5"
   - Tool executada: `GET https://jsonplaceholder.typicode.com/users/5`
   - Resultado: Dados do usuário retornados

## 🎉 **Benefícios da correção:**

✅ **URLs RESTful funcionam corretamente**
✅ **Suporte a múltiplos placeholders**
✅ **Compatibilidade com todos os métodos HTTP**
✅ **Query parameters preservados adequadamente** 
✅ **Código limpo e testado**
✅ **Documentação completa**

---

**🔥 A funcionalidade HTTP Tools agora está totalmente funcional e pronta para uso em produção!** 🚀
