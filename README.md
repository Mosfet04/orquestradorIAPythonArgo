# 🤖 AI Agents Orchestrator / Orquestrador de Agentes IA

<div align="center">

![Python](https://img.shields.io/badge/python-v3.9+-blue.svg)
![FastAPI](https://img.shields.io/badge/FastAPI-005571?style=for-the-badge&logo=fastapi)
![MongoDB](https://img.shields.io/badge/MongoDB-%234ea94b.svg?style=for-the-badge&logo=mongodb&logoColor=white)
![License](https://img.shields.io/badge/license-MIT-green.svg)

*A robust Python application implementing an AI agents orchestrator using Onion Architecture (Clean Architecture) and Clean Code principles*

**📖 Full Documentation**

🇧🇷 **[Documentação em Português](README.pt-br.md)** | 🇺🇸 **[English Documentation](README.en.md)**

</div>

## 🚀 Quick Start

```bash
# Clone and run
git clone https://github.com/your-username/orquestradorIAPythonArgo.git
cd orquestradorIAPythonArgo
pip install -r requirements.txt
python app.py
```

**Access:**
- 🌐 API Documentation: http://localhost:7777/docs
- 🎮 Interactive Playground: http://localhost:7777/playground
- ❤️ Health Check: http://localhost:7777/health

## 🏗️ Architecture Overview

```mermaid
graph TB
    subgraph "🎯 Domain"
        E[Entities]
        R[Repositories]
    end
    
    subgraph "📋 Application"
        UC[Use Cases]
        S[Services]
    end
    
    subgraph "🔧 Infrastructure"
        DB[(MongoDB)]
        HTTP[HTTP Tools]
    end
    
    subgraph "🌐 Presentation"
        API[FastAPI]
        PG[Playground]
    end
    
    API --> UC
    PG --> UC
    UC --> S
    S --> E
    S --> R
    DB --> R
    HTTP --> R
```

## ✨ Key Features

- 🤖 **Multi-Agent Management** with RAG support
- 🛠️ **Custom Tools Integration** via HTTP APIs
- 🧠 **Multiple AI Model Providers** (Ollama, OpenAI, etc.)
- 🎮 **Interactive Web Playground**
- 🌐 **RESTful API** with FastAPI
- 📊 **Structured Logging** and observability
- 🧪 **Comprehensive Testing** suite
- 🏗️ **Clean Architecture** implementation

## 📚 Documentation

For complete documentation, choose your language:

### 🇧🇷 Português
- **[README Completo em Português](README.pt-br.md)** - Documentação detalhada em português
- Inclui guias para desenvolvedores iniciantes e experientes
- Diagramas de arquitetura e fluxo de dados
- Exemplos práticos e configurações

### 🇺🇸 English
- **[Complete English README](README.en.md)** - Detailed documentation in English
- Includes guides for junior and senior developers
- Architecture and data flow diagrams
- Practical examples and configurations

## 🤝 Contributing

Contributions are welcome! Please read our documentation for guidelines on how to contribute to this project.

## 📄 License

This project is licensed under the MIT License - see the [LICENSE](LICENSE) file for details.

---

<div align="center">

**Made with ❤️ using Clean Architecture principles**

</div>
