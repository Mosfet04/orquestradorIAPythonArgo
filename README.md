# Orquestrador de Agentes IA

Uma aplicação Python que implementa um orquestrador de agentes de IA utilizando arquitetura Onion (Clean Architecture) e princípios de Clean Code.

## Arquitetura

A aplicação segue os princípios da arquitetura Onion, organizando o código em camadas bem definidas:

```
src/
├── domain/                     # Camada de Domínio (núcleo)
│   ├── entities/              # Entidades de negócio
│   │   └── agent_config.py    # Configuração de agente
│   └── repositories/          # Interfaces de repositórios
│       └── agent_config_repository.py
├── application/               # Camada de Aplicação
│   ├── services/             # Serviços de aplicação
│   │   └── agent_factory_service.py
│   └── use_cases/            # Casos de uso
│       └── get_active_agents_use_case.py
├── infrastructure/           # Camada de Infraestrutura
│   ├── config/              # Configurações
│   │   └── app_config.py
│   ├── repositories/        # Implementações concretas
│   │   └── mongo_agent_config_repository.py
│   └── dependency_injection.py
└── presentation/            # Camada de Apresentação
    └── controllers/         # Controllers
        └── orquestrador_controller.py
```

## Princípios Aplicados

### Clean Code
- **Nomes expressivos**: Classes, métodos e variáveis com nomes que expressam claramente sua intenção
- **Funções pequenas**: Cada função tem uma única responsabilidade
- **Comentários mínimos**: O código é autoexplicativo
- **Tratamento de erros**: Validações claras e exceções específicas

### Arquitetura Onion
- **Inversão de dependências**: As camadas externas dependem das internas
- **Separação de responsabilidades**: Cada camada tem uma responsabilidade específica
- **Testabilidade**: Facilita a criação de testes unitários e de integração
- **Flexibilidade**: Permite mudanças de infraestrutura sem afetar a lógica de negócio

## Camadas da Arquitetura

### 1. Domain (Domínio)
- **Entidades**: Objetos de negócio puros, sem dependências externas
- **Repositórios**: Interfaces que definem contratos para acesso a dados
- **Regras de negócio**: Validações e lógicas centrais da aplicação

### 2. Application (Aplicação)
- **Use Cases**: Orquestram o fluxo de dados e coordenam entidades
- **Services**: Serviços de aplicação que implementam lógicas específicas
- **Não conhece detalhes de infraestrutura**

### 3. Infrastructure (Infraestrutura)
- **Implementações concretas**: Repositórios, conexões com banco de dados
- **Configurações**: Gerenciamento de configurações da aplicação
- **Injeção de dependências**: Container para gerenciar dependências

### 4. Presentation (Apresentação)
- **Controllers**: Pontos de entrada da aplicação
- **APIs**: Interfaces REST ou outras formas de comunicação
- **Formatação de dados**: Conversão entre formatos internos e externos

## Funcionalidades

A aplicação mantém todas as funcionalidades originais:

- ✅ Busca agentes configurados no MongoDB
- ✅ Cria instâncias de agentes com configurações específicas
- ✅ Playground para interação com múltiplos agentes
- ✅ API FastAPI para consumo dos agentes
- ✅ Montagem de sub-aplicações nas rotas `/playground` e `/api`

## Como executar

1. Instalar dependências:
```bash
pip install -r requirements.txt
```

2. Configurar variáveis de ambiente (opcional):
```bash
export MONGO_CONNECTION_STRING="mongodb://localhost:27017"
export MONGO_DATABASE_NAME="agno"
export APP_TITLE="Orquestrador agno"
```

3. Executar a aplicação:
```bash
python app.py
```

## Testes

Execute os testes unitários:
```bash
pytest tests/unit/ -v
```

Execute os testes de integração:
```bash
pytest tests/integration/ -v
```

Execute todos os testes:
```bash
pytest tests/ -v
```

## Benefícios da Nova Arquitetura

1. **Manutenibilidade**: Código organizado e fácil de entender
2. **Testabilidade**: Cada camada pode ser testada independentemente
3. **Flexibilidade**: Fácil troca de componentes de infraestrutura
4. **Escalabilidade**: Estrutura que suporta crescimento da aplicação
5. **Reusabilidade**: Componentes podem ser reutilizados em diferentes contextos

## Estrutura de Dados

### AgentConfig
```python
@dataclass
class AgentConfig:
    id: str
    nome: str
    model: str
    descricao: str
    prompt: str
    active: bool = True
```

### Configuração do MongoDB
Os agentes devem estar armazenados na coleção `agents_config` com a seguinte estrutura:
```json
{
    "id": "agent-1",
    "nome": "Assistente Geral",
    "model": "llama3.2:latest",
    "descricao": "Um assistente para tarefas gerais",
    "prompt": "Você é um assistente útil...",
    "active": true
}
```
