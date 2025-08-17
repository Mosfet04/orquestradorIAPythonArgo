// MongoDB initialization script
// This script creates sample data for the AI Agents Orchestrator

// Switch to agno database
if (typeof db === 'undefined') {
  var db = connect('localhost:27017/agno');
} else {
  db = db.getSiblingDB('agno');
}

// Create agents_config collection with sample agents
db.agents_config.insertMany([
  {
    "id": "general-assistant",
    "nome": "General Assistant",
    "model": "llama3.2:latest",
    "factoryIaModel": "ollama",
    "descricao": "A general purpose AI assistant that can help with various tasks",
    "prompt": "You are a helpful AI assistant. You can help users with a wide variety of tasks including answering questions, writing, analysis, and problem-solving. Always be polite, accurate, and helpful.",
    "active": true,
    "tools_ids": [],
    "rag_config": {
      "active": false
    }
  },
  {
    "id": "code-assistant",
    "nome": "Code Assistant",
    "model": "llama3.2:latest",
    "factoryIaModel": "ollama",
    "descricao": "Specialized AI assistant for programming and software development",
    "prompt": "You are an expert programming assistant. You help developers with code review, debugging, writing clean code, explaining complex programming concepts, and providing best practices. You are knowledgeable in multiple programming languages and frameworks.",
    "active": true,
    "tools_ids": [],
    "rag_config": {
      "active": false
    }
  },
  {
    "id": "analyst-assistant",
    "nome": "Data Analyst Assistant",
    "model": "llama3.2:latest",
    "factoryIaModel": "ollama",
    "descricao": "AI assistant specialized in data analysis and business intelligence",
    "prompt": "You are a data analysis expert. You help users understand data, create insights, suggest analytical approaches, and explain statistical concepts. You can help with data visualization recommendations and business intelligence strategies.",
    "active": true,
    "tools_ids": ["weather-tool"],
    "rag_config": {
      "active": false
    }
  }
]);

// Create tools_config collection with sample tools
db.tools_config.insertMany([
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

// Create indexes for better performance
db.agents_config.createIndex({ "id": 1 }, { unique: true });
db.agents_config.createIndex({ "active": 1 });
db.agents_config.createIndex({ "factoryIaModel": 1 });

db.tools_config.createIndex({ "id": 1 }, { unique: true });
db.tools_config.createIndex({ "name": 1 });

print("Database initialized with sample data!");
print("Created agents:", db.agents_config.countDocuments({}));
print("Created tools:", db.tools_config.countDocuments({}));
