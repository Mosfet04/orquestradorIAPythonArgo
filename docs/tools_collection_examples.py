# Estrutura de exemplo para collection "tools" no MongoDB

# Documento de exemplo 1: Tool para buscar informações de usuário
{
    "id": "get-user-info",
    "name": "Get User Info", 
    "description": "Busca informações detalhadas de um usuário",
    "route": "https://api.example.com/users/{user_id}",
    "http_method": "GET",
    "parameters": [
        {
            "name": "user_id",
            "type": "string",
            "description": "ID único do usuário",
            "required": True,
            "default_value": None
        },
        {
            "name": "include_profile",
            "type": "boolean", 
            "description": "Se deve incluir dados do perfil",
            "required": False,
            "default_value": True
        }
    ],
    "instructions": "Use esta ferramenta para obter dados completos de um usuário. Sempre informe o user_id.",
    "headers": {
        "Authorization": "Bearer YOUR_API_TOKEN",
        "Content-Type": "application/json"
    },
    "active": True
}

# Documento de exemplo 2: Tool para criar novo usuário
{
    "id": "create-user",
    "name": "Create User",
    "description": "Cria um novo usuário no sistema",
    "route": "https://api.example.com/users",
    "http_method": "POST",
    "parameters": [
        {
            "name": "name",
            "type": "string",
            "description": "Nome completo do usuário",
            "required": True,
            "default_value": None
        },
        {
            "name": "email",
            "type": "string",
            "description": "Email válido do usuário",
            "required": True,
            "default_value": None
        },
        {
            "name": "age",
            "type": "integer",
            "description": "Idade do usuário",
            "required": False,
            "default_value": 18
        },
        {
            "name": "preferences",
            "type": "object",
            "description": "Preferências do usuário",
            "required": False,
            "default_value": {}
        }
    ],
    "instructions": "Esta ferramenta cria um novo usuário. Name e email são obrigatórios. Sempre valide os dados antes de enviar.",
    "headers": {
        "Authorization": "Bearer YOUR_API_TOKEN",
        "Content-Type": "application/json"
    },
    "active": True
}

# Documento de exemplo 3: Tool para buscar clima
{
    "id": "get-weather",
    "name": "Get Weather",
    "description": "Consulta informações do clima atual",
    "route": "https://api.openweathermap.org/data/2.5/weather",
    "http_method": "GET", 
    "parameters": [
        {
            "name": "q",
            "type": "string",
            "description": "Nome da cidade",
            "required": True,
            "default_value": None
        },
        {
            "name": "units",
            "type": "string",
            "description": "Unidade de temperatura (metric, imperial, kelvin)",
            "required": False,
            "default_value": "metric"
        },
        {
            "name": "appid",
            "type": "string",
            "description": "API key do OpenWeatherMap",
            "required": True,
            "default_value": "YOUR_WEATHER_API_KEY"
        }
    ],
    "instructions": "Use para obter dados meteorológicos. Sempre forneça o nome da cidade. A API retorna temperatura, umidade, descrição do tempo.",
    "headers": {},
    "active": True
}

# Documento de exemplo 4: Tool desabilitada
{
    "id": "deprecated-tool",
    "name": "Deprecated Tool",
    "description": "Esta ferramenta foi descontinuada",
    "route": "https://api.example.com/deprecated",
    "http_method": "GET",
    "parameters": [],
    "instructions": "Esta ferramenta não deve mais ser usada",
    "headers": {},
    "active": False
}
