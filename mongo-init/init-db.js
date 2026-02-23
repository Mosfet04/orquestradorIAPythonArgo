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
//    NOTE: collection name is "tools", matching the repository
// ============================================================
db.tools.insertMany([
  {
    "id": "weather-tool",
    "name": "Weather Information",
    "description": "Get current weather information for any city",
    "http_config": {
      "base_url": "https://api.openweathermap.org/data/2.5",
      "method": "GET",
      "endpoint": "/weather",
      "headers": {
        "Content-Type": "application/json"
      },
      "parameters": [
        {
          "name": "q",
          "type": "string",
          "description": "City name",
          "required": true
        },
        {
          "name": "appid",
          "type": "string",
          "description": "API key",
          "required": true
        },
        {
          "name": "units",
          "type": "string",
          "description": "Temperature units (metric, imperial, kelvin)",
          "required": false,
          "default": "metric"
        }
      ]
    }
  },
  {
    "id": "calculator-tool",
    "name": "Calculator",
    "description": "Perform mathematical calculations",
    "http_config": {
      "base_url": "https://api.mathjs.org/v4",
      "method": "GET",
      "endpoint": "/",
      "headers": {
        "Content-Type": "application/json"
      },
      "parameters": [
        {
          "name": "expr",
          "type": "string",
          "description": "Mathematical expression to evaluate",
          "required": true
        }
      ]
    }
  }
]);

db.tools.createIndex({ "id": 1 }, { unique: true });
db.tools.createIndex({ "name": 1 });

// ============================================================
// 3. teams_config — Team configurations
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
    "memberIds": ["general-assistant", "code-assistant", "analyst-assistant"],
    "userMemoryActive": true,
    "summaryActive": false,
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
// 6. agno_traces — Execution traces
// ============================================================
db.createCollection("agno_traces");
db.agno_traces.createIndex({ "trace_id": 1 }, { unique: true });
db.agno_traces.createIndex({ "session_id": 1 });
db.agno_traces.createIndex({ "agent_id": 1 });
db.agno_traces.createIndex({ "team_id": 1 });
db.agno_traces.createIndex({ "created_at": -1 });

// ============================================================
// 7. agno_spans — Execution spans (sub-traces)
// ============================================================
db.createCollection("agno_spans");
db.agno_spans.createIndex({ "span_id": 1 }, { unique: true });
db.agno_spans.createIndex({ "trace_id": 1 });
db.agno_spans.createIndex({ "created_at": -1 });

// ============================================================
// 8. rag — Vector DB collection for RAG embeddings
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
print("Pre-created agno runtime collections: agno_sessions, agno_memories, agno_traces, agno_spans, rag");
