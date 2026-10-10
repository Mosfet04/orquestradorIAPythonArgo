// MongoDB initialization script
// This script creates sample data for the AI Agents Orchestrator

// Switch to agno database
db = db.getSiblingDB('agno');

// ============================================================
// 1. agents_config — Agent configurations
// ============================================================
db.agents_config.insertMany([
  {
    "id": "general-assistant",
    "nome": "General Assistant",
    "model": "qwen3",
    "factoryIaModel": "ollama",
    "descricao": "A general purpose AI assistant that can help with various tasks",
    "prompt": [
      "You are a helpful AI assistant. You can help users with a wide variety of tasks including answering questions, writing, analysis, and problem-solving. Always be polite, accurate, and helpful."
    ],
    "tools_ids": [],
    "rag_config": {
      "active": false
    },
    "user_memory_active": false,
    "summary_active": false,
    "active": true,
    "updated_at": new Date()
  },
  {
    "id": "code-assistant",
    "nome": "Code Assistant",
    "model": "qwen3",
    "factoryIaModel": "ollama",
    "descricao": "Specialized AI assistant for programming and software development",
    "prompt": [
      "You are an expert programming assistant. You help developers with code review, debugging, writing clean code, explaining complex programming concepts, and providing best practices. You are knowledgeable in multiple programming languages and frameworks."
    ],
    "tools_ids": [],
    "rag_config": {
      "active": false
    },
    "user_memory_active": false,
    "summary_active": false,
    "active": true,
    "updated_at": new Date()
  },
  {
    "id": "analyst-assistant",
    "nome": "Data Analyst Assistant",
    "model": "qwen3",
    "factoryIaModel": "ollama",
    "descricao": "AI assistant specialized in data analysis and business intelligence",
    "prompt": [
      "You are a data analysis expert. You help users understand data, create insights, suggest analytical approaches, and explain statistical concepts. You can help with data visualization recommendations and business intelligence strategies."
    ],
    "tools_ids": ["weather-tool"],
    "rag_config": {
      "active": false
    },
    "user_memory_active": false,
    "summary_active": false,
    "active": true,
    "updated_at": new Date()
  }
]);

db.agents_config.createIndex({ "id": 1 }, { unique: true });
db.agents_config.createIndex({ "active": 1 });
db.agents_config.createIndex({ "factoryIaModel": 1 });

// ============================================================
// 2. tools — HTTP tool configurations
//    Formato lido por MongoToolRepository (src/infrastructure/repositories/
//    mongo_tool_repository.py): route com {param} para parâmetro de rota,
//    http_method, parameters[{name, type, description, required}], instructions
//    (vão para o system message) e active=true (só tools ativas são buscadas).
//    type: string | integer | float | boolean | object | array.
//    Segredo (API key, token) NUNCA vai como parâmetro do LLM nem em texto no
//    documento: headers secretos por referência (env:VAR) chegam na F5-05.
//    As tools de exemplo usam APIs públicas sem chave.
//    O array abaixo é JSON puro (tests/unit/test_seed_tools.py o lê com json):
//    sem comentário, new Date() ou vírgula sobrando dentro dele.
// ============================================================
db.tools.insertMany([
  {
    "id": "weather-tool",
    "name": "Weather Information",
    "description": "Get current weather for a location (Open-Meteo, no API key)",
    "route": "https://api.open-meteo.com/v1/forecast",
    "http_method": "GET",
    "parameters": [
      {
        "name": "latitude",
        "type": "float",
        "description": "Latitude in decimal degrees (e.g. -23.55 for São Paulo)",
        "required": true
      },
      {
        "name": "longitude",
        "type": "float",
        "description": "Longitude in decimal degrees (e.g. -46.63 for São Paulo)",
        "required": true
      },
      {
        "name": "current",
        "type": "string",
        "description": "Comma-separated current variables, e.g. temperature_2m,relative_humidity_2m,wind_speed_10m",
        "required": true
      }
    ],
    "instructions": "Use for current weather questions. Convert the city to approximate latitude/longitude yourself and pass current=temperature_2m,relative_humidity_2m,wind_speed_10m.",
    "headers": {},
    "active": true
  },
  {
    "id": "calculator-tool",
    "name": "Calculator",
    "description": "Evaluate a mathematical expression (mathjs.org)",
    "route": "https://api.mathjs.org/v4/",
    "http_method": "GET",
    "parameters": [
      {
        "name": "expr",
        "type": "string",
        "description": "Mathematical expression to evaluate, e.g. 2*(3+4)",
        "required": true
      }
    ],
    "instructions": "Use for arithmetic that must be exact; pass the whole expression in expr.",
    "headers": {},
    "active": true
  }
]);

db.tools.createIndex({ "id": 1 }, { unique: true });
db.tools.createIndex({ "name": 1 });

// ============================================================
// 3. teams_config — Team configurations
//    Formato canônico (o mesmo dos agentes e do README): factoryIaModel e o
//    resto em snake_case (member_ids, user_memory_active, summary_active).
//    MongoTeamConfigRepository ainda lê o legado camelCase (memberIds,
//    userMemoryActive, summaryActive) de documentos antigos; com os dois no
//    mesmo documento vale o snake_case. Documento inválido é ignorado com
//    log de erro (id + tipo do erro) e os demais carregam.
//    O array abaixo é JSON puro (tests/unit/test_seed_teams.py o lê com json).
// ============================================================
db.teams_config.insertMany([
  {
    "id": "support-router",
    "nome": "Support Router",
    "mode": "route",
    "model": "qwen3",
    "factoryIaModel": "ollama",
    "descricao": "Routes user requests to the most appropriate specialist agent",
    "prompt": "You are a smart router. Analyze the user's message and delegate it to the most appropriate team member. For programming questions use the code-assistant, for data analysis use the analyst-assistant, and for general questions use the general-assistant.",
    "member_ids": ["general-assistant", "code-assistant", "analyst-assistant"],
    "user_memory_active": true,
    "summary_active": false,
    "active": true
  }
]);

db.teams_config.createIndex({ "id": 1 }, { unique: true });
db.teams_config.createIndex({ "active": 1 });

// ============================================================
// 4. agno_sessions — Created automatically by agno's MongoDb
//    storage, but we pre-create indexes for performance
// ============================================================
db.createCollection("agno_sessions");
db.agno_sessions.createIndex({ "session_id": 1 }, { unique: true });
db.agno_sessions.createIndex({ "user_id": 1 });
db.agno_sessions.createIndex({ "agent_id": 1 });
db.agno_sessions.createIndex({ "team_id": 1 });
db.agno_sessions.createIndex({ "created_at": -1 });

// ============================================================
// 5. agno_memories — User memories persisted by agno
// ============================================================
db.createCollection("agno_memories");
db.agno_memories.createIndex({ "memory_id": 1 }, { unique: true });
db.agno_memories.createIndex({ "user_id": 1 });
db.agno_memories.createIndex({ "agent_id": 1 });
db.agno_memories.createIndex({ "created_at": -1 });

// ============================================================
// 6. rag — Vector DB collection for RAG embeddings
//    (traces/spans removidos — agora exportados via OTLP
//     para Grafana Tempo)
// ============================================================
db.createCollection("rag");
db.rag.createIndex({ "name": 1 });
db.rag.createIndex({ "content_hash": 1 });

// ============================================================
// Summary
// ============================================================
print("Database initialized with sample data!");
print("Created agents:", db.agents_config.countDocuments({}));
print("Created tools:", db.tools.countDocuments({}));
print("Created teams:", db.teams_config.countDocuments({}));
print("Pre-created agno runtime collections: agno_sessions, agno_memories, rag");
print("Traces/spans are now exported via OTLP to Grafana Tempo");
