# 🐛 Guia de Debug e Depuração

Este guia explica como usar as configurações de debug criadas para depurar e testar a aplicação.

## 🚀 Configurações de Debug Disponíveis

### 1. **🚀 Debug App Principal**
- **Uso**: Debug da aplicação FastAPI principal
- **Arquivo**: `app.py`
- **Porta**: 7777
- **Variáveis**: Ambiente de debug configurado

### 2. **🧪 Debug Testes Unitários**
- **Uso**: Executa todos os testes unitários em modo debug
- **Pasta**: `tests/unit/`
- **Útil para**: Debug de falhas em testes específicos

### 3. **🔬 Debug Teste Específico**
- **Uso**: Debug do arquivo de teste atual
- **Como usar**: Abra um arquivo de teste e execute esta configuração
- **Útil para**: Debug de um teste específico

### 4. **🧩 Debug Testes de Integração**
- **Uso**: Executa testes de integração
- **Pasta**: `tests/integration/`
- **Database**: `agno_test` (separado do desenvolvimento)

### 5. **📊 Debug com Cobertura**
- **Uso**: Executa testes com relatório de cobertura
- **Saída**: Terminal + arquivo HTML
- **Útil para**: Verificar cobertura de testes

### 6. **🔧 Debug AgentFactoryService**
- **Uso**: Testa criação de agentes
- **Script**: `debug_scripts/test_agent_factory.py`
- **Testa**: Agentes com e sem tools

### 7. **🛠️ Debug Tool Repository**
- **Uso**: Testa busca de tools no MongoDB
- **Script**: `debug_scripts/test_tool_repository.py`
- **Testa**: Conexão MongoDB e busca de tools

### 8. **⚡ Debug FastAPI com Uvicorn**
- **Uso**: Debug usando uvicorn diretamente
- **Modo**: Reload ativo, logs debug
- **Útil para**: Debug de problemas de inicialização

### 9. **🐛 Debug Completo (Include Libraries)**
- **Uso**: Debug incluindo bibliotecas externas
- **Configuração**: `justMyCode: false`
- **Útil para**: Debug de problemas em dependências

### 10. **📝 Debug Script Personalizado**
- **Uso**: Debug do arquivo Python atual
- **Como usar**: Abra qualquer `.py` e execute
- **Útil para**: Debug de scripts personalizados

## 🔧 Scripts de Debug Específicos

### `test_agent_factory.py`
Testa a criação de agentes com e sem tools:
```bash
# Executa diretamente
python debug_scripts/test_agent_factory.py

# Ou use a configuração de debug "🔧 Debug AgentFactoryService"
```

### `test_tool_repository.py`
Testa a busca de tools no MongoDB:
```bash
# Executa diretamente  
python debug_scripts/test_tool_repository.py

# Ou use a configuração de debug "🛠️ Debug Tool Repository"
```

### `test_app_complete.py`
Testa a aplicação completa:
```bash
# Executa diretamente
python debug_scripts/test_app_complete.py
```

## 📋 Preparação do Ambiente

### 1. **Variáveis de Ambiente**
Copie e configure o arquivo de ambiente:
```bash
cp .env.example .env
# Edite o .env com suas configurações
```

### 2. **MongoDB Local**
Certifique-se de que o MongoDB está rodando:
```bash
# Windows (se instalado como serviço)
net start MongoDB

# Ou usando Docker
docker run -d -p 27017:27017 --name mongodb mongo:latest
```

### 3. **Ambiente Python**
Certifique-se de que o ambiente virtual está ativo:
```bash
# Windows
.venv\Scripts\activate

# VS Code deve ativar automaticamente
```

## 🎯 Como Usar o Debug

### **Método 1: Via VS Code Debug Panel**
1. Abra o painel Debug (`Ctrl+Shift+D`)
2. Selecione a configuração desejada
3. Clique em "▶️ Start Debugging" ou pressione `F5`

### **Método 2: Via Command Palette**
1. Pressione `Ctrl+Shift+P`
2. Digite "Debug: Start Debugging"
3. Selecione a configuração

### **Método 3: Breakpoints**
1. Clique na margem esquerda do editor para adicionar breakpoints
2. Execute qualquer configuração de debug
3. A execução pausará nos breakpoints

## 🛠️ Dicas de Debug

### **Breakpoints Efetivos**
- **Linha específica**: Clique na margem esquerda
- **Conditional**: Clique direito no breakpoint > "Edit Breakpoint"
- **Log points**: Para imprimir valores sem parar

### **Navegação Durante Debug**
- **F5**: Continue
- **F10**: Step Over (próxima linha)
- **F11**: Step Into (entra na função)
- **Shift+F11**: Step Out (sai da função)
- **Ctrl+F5**: Run Without Debugging

### **Painéis Úteis**
- **Variables**: Visualiza variáveis locais e globais
- **Watch**: Monitora expressões específicas
- **Call Stack**: Mostra a pilha de chamadas
- **Debug Console**: Execute código Python durante debug

### **Comandos no Debug Console**
```python
# Visualizar variáveis
print(agent_config)

# Executar métodos
agent_factory.create_agent(config)

# Avaliar expressões
len(tools) > 0

# Importar módulos
import json; print(json.dumps(config.__dict__))
```

## 🔍 Troubleshooting

### **Problema: "Module not found"**
- Verifique se `PYTHONPATH` está definido
- Confirme que o ambiente virtual está ativo
- Verifique imports relativos vs absolutos

### **Problema: "MongoDB connection error"**
- Confirme que MongoDB está rodando na porta 27017
- Verifique as variáveis de ambiente
- Teste conexão: `mongo --eval "db.runCommand({connectionStatus: 1})"`

### **Problema: "Tools não encontradas"**
- Verifique se a collection `tools` existe
- Use o script `test_tool_repository.py` para diagnosticar
- Adicione dados de exemplo usando `docs/tools_collection_examples.py`

### **Problema: "Breakpoints não funcionam"**
- Certifique-se de que está usando a configuração correta
- Verifique se `justMyCode` está configurado corretamente
- Tente recarregar o VS Code

## 📚 Recursos Adicionais

- **[VS Code Python Debugging](https://code.visualstudio.com/docs/python/debugging)**
- **[pytest Documentation](https://docs.pytest.org/)**
- **[FastAPI Debugging](https://fastapi.tiangolo.com/tutorial/debugging/)**
- **[agno Framework](https://github.com/agno-org/agno)**

## 💡 Fluxo Recomendado de Debug

1. **Teste Unitário**: Primeiro debug com testes unitários
2. **Componente Específico**: Use scripts de debug específicos
3. **Integração**: Teste componentes integrados
4. **Aplicação Completa**: Debug da aplicação toda
5. **Produção**: Use logs e monitoramento

---

**Happy Debugging! 🐛🔍**
