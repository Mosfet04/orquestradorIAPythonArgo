# Contributing to AI Agents Orchestrator

🎉 Thank you for your interest in contributing to the AI Agents Orchestrator! This document provides guidelines and information for contributors.

## 📋 Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [Contributing Guidelines](#contributing-guidelines)
- [Code Standards](#code-standards)
- [Testing](#testing)
- [Documentation](#documentation)
- [Pull Request Process](#pull-request-process)
- [Issue Reporting](#issue-reporting)

## 📜 Code of Conduct

This project adheres to a code of conduct. By participating, you are expected to uphold this code. Please report unacceptable behavior to the project maintainers.

### Our Pledge

- Be respectful and inclusive
- Welcome newcomers and help them learn
- Focus on what is best for the community
- Show empathy towards other community members

## 🚀 Getting Started

### Prerequisites

- Python 3.9 or higher
- MongoDB 4.4 or higher
- Git
- Basic understanding of Clean Architecture principles

### Quick Setup

1. **Fork the repository**
   ```bash
   git clone https://github.com/your-username/orquestradorIAPythonArgo.git
   cd orquestradorIAPythonArgo
   ```

2. **Run setup script**
   ```bash
   # Linux/macOS
   ./setup.sh all
   
   # Windows
   .\setup.ps1 all
   ```

3. **Create a feature branch**
   ```bash
   git checkout -b feature/your-feature-name
   ```

## 🛠️ Development Setup

### Manual Setup

1. **Virtual Environment**
   ```bash
   python -m venv venv
   source venv/bin/activate  # Linux/macOS
   # or
   .\venv\Scripts\Activate.ps1  # Windows
   ```

2. **Install Dependencies**
   ```bash
   pip install -r requirements.txt
   pip install -r requirements-dev.txt  # Development dependencies
   ```

3. **Environment Configuration**
   ```bash
   cp .env.development .env
   # Edit .env with your configurations
   ```

4. **Database Setup**
   ```bash
   # Start MongoDB (if not using Docker)
   mongod
   
   # Initialize with sample data
   mongosh agno mongo-init/init-db.js
   ```

### Using Docker

```bash
# Start all services
docker-compose up -d

# View logs
docker-compose logs -f app

# Stop services
docker-compose down
```

## 📝 Contributing Guidelines

### Types of Contributions

We welcome contributions in these areas:

- 🐛 **Bug fixes**
- ✨ **New features**
- 📚 **Documentation improvements**
- 🧪 **Test coverage**
- 🔧 **Performance optimizations**
- 🌐 **Internationalization**

### Contribution Workflow

1. **Check existing issues** before creating new ones
2. **Discuss major changes** in an issue first
3. **Write tests** for new functionality
4. **Update documentation** as needed
5. **Follow code standards** (see below)
6. **Submit a pull request**

## 🎯 Code Standards

### Architecture Principles

This project follows **Clean Architecture (Onion Architecture)**:

```
📁 Domain Layer (Core)
  ├── Entities (business objects)
  ├── Repository interfaces
  └── Domain services

📁 Application Layer
  ├── Use cases
  ├── Application services
  └── DTOs

📁 Infrastructure Layer
  ├── Database implementations
  ├── External services
  └── Configuration

📁 Presentation Layer
  ├── Controllers
  ├── API endpoints
  └── UI components
```

### Code Style

We use these tools for code quality:

- **Black** for code formatting
- **isort** for import sorting
- **flake8** for linting
- **mypy** for type checking

Run before committing:
```bash
# Format code
black .
isort .

# Check linting
flake8 .

# Type checking
mypy src/
```

### Python Standards

#### Naming Conventions
```python
# ✅ Good
class AgentFactoryService:
    def create_agent(self, config: AgentConfig) -> Agent:
        pass

# ❌ Bad
class agentFactory:
    def createagent(self, cfg):
        pass
```

#### Type Hints
```python
# ✅ Good
def get_agents(self, active_only: bool = True) -> List[AgentConfig]:
    return self._repository.find_active() if active_only else self._repository.find_all()

# ❌ Bad
def get_agents(self, active_only=True):
    return self._repository.find_active() if active_only else self._repository.find_all()
```

#### Error Handling
```python
# ✅ Good
class AgentNotFoundError(Exception):
    def __init__(self, agent_id: str):
        super().__init__(f"Agent with ID '{agent_id}' not found")
        self.agent_id = agent_id

# ❌ Bad
raise Exception("Agent not found")
```

### Clean Code Principles

1. **Single Responsibility**: Each class/function has one reason to change
2. **Open/Closed**: Open for extension, closed for modification
3. **Dependency Inversion**: Depend on abstractions, not concretions
4. **Meaningful Names**: Use intention-revealing names
5. **Small Functions**: Keep functions small and focused

## 🧪 Testing

### Testing Strategy

We use **pytest** with the following test types:

```
tests/
├── unit/           # Fast, isolated tests
├── integration/    # Tests with external dependencies
└── e2e/           # End-to-end tests
```

### Writing Tests

#### Unit Tests
```python
# tests/unit/domain/test_agent_config.py
import pytest
from src.domain.entities.agent_config import AgentConfig

def test_agent_config_creation():
    config = AgentConfig(
        id="test-agent",
        nome="Test Agent",
        model="llama3.2:latest",
        factoryIaModel="ollama",
        descricao="Test description",
        prompt="Test prompt"
    )
    assert config.id == "test-agent"
    assert config.active is True  # default value

def test_agent_config_validation():
    with pytest.raises(ValueError, match="ID do agente não pode estar vazio"):
        AgentConfig(
            id="",
            nome="Test",
            model="model",
            factoryIaModel="ollama",
            descricao="desc",
            prompt="prompt"
        )
```

#### Integration Tests
```python
# tests/integration/test_agent_repository.py
import pytest
from src.infrastructure.repositories.mongo_agent_config_repository import MongoAgentConfigRepository

@pytest.mark.integration
def test_get_active_agents(mongo_client):
    repository = MongoAgentConfigRepository(mongo_client, "test_db")
    agents = repository.get_active_agents()
    assert len(agents) > 0
    assert all(agent.active for agent in agents)
```

### Running Tests

```bash
# All tests
pytest

# Unit tests only
pytest tests/unit/ -v

# Integration tests only
pytest tests/integration/ -v

# With coverage
pytest --cov=src --cov-report=html

# Specific test file
pytest tests/unit/domain/test_agent_config.py -v
```

### Test Requirements

- **Coverage**: Maintain >80% code coverage
- **Fast**: Unit tests should run in <1s each
- **Isolated**: Tests should not depend on each other
- **Descriptive**: Test names should describe what they test

## 📚 Documentation

### Documentation Types

1. **Code Documentation**: Docstrings for all public methods
2. **API Documentation**: Automatic OpenAPI/Swagger docs
3. **Architecture Documentation**: High-level design docs
4. **User Documentation**: README and guides

### Docstring Format

```python
def create_agent(self, config: AgentConfig) -> Agent:
    """
    Create a new agent instance based on the provided configuration.
    
    Args:
        config: The agent configuration containing model, prompt, and other settings.
        
    Returns:
        A configured agent instance ready for use.
        
    Raises:
        ValueError: If the configuration is invalid.
        ModelNotFoundError: If the specified model is not available.
        
    Example:
        >>> config = AgentConfig(id="test", nome="Test", ...)
        >>> agent = service.create_agent(config)
        >>> agent.chat("Hello")
    """
```

### API Documentation

All API endpoints are automatically documented using FastAPI's OpenAPI integration. Ensure your endpoint functions have proper docstrings and type hints.

## 🔄 Pull Request Process

### Before Submitting

1. **Update documentation** if needed
2. **Add tests** for new functionality
3. **Run the test suite** and ensure all tests pass
4. **Check code quality** with linting tools
5. **Update CHANGELOG.md** if applicable

### PR Template

When submitting a PR, please include:

```markdown
## Description
Brief description of changes.

## Type of Change
- [ ] Bug fix
- [ ] New feature
- [ ] Documentation update
- [ ] Performance improvement
- [ ] Refactoring

## Testing
- [ ] Unit tests added/updated
- [ ] Integration tests added/updated
- [ ] All tests pass locally

## Documentation
- [ ] Code documented
- [ ] API docs updated
- [ ] README updated if needed

## Screenshots (if applicable)
Add screenshots for UI changes.
```

### Review Process

1. **Automated checks** must pass (CI/CD)
2. **Code review** by at least one maintainer
3. **Manual testing** for significant changes
4. **Documentation review** if docs are updated

## 🐛 Issue Reporting

### Bug Reports

Use the bug report template and include:

- **Environment details** (OS, Python version, etc.)
- **Steps to reproduce** the issue
- **Expected vs actual behavior**
- **Error messages** and stack traces
- **Screenshots** if applicable

### Feature Requests

Use the feature request template and include:

- **Problem description** you're trying to solve
- **Proposed solution** with examples
- **Alternatives considered**
- **Implementation considerations**

### Issue Labels

We use these labels:

- `bug`: Something isn't working
- `enhancement`: New feature or request
- `documentation`: Improvements or additions to docs
- `good first issue`: Good for newcomers
- `help wanted`: Extra attention is needed
- `priority-high`: High priority issue

## 🏆 Recognition

Contributors will be:

- **Listed in CONTRIBUTORS.md**
- **Mentioned in release notes**
- **Given credit in documentation**

## 📞 Getting Help

- **Discord**: [Join our Discord server](discord-link)
- **Discussions**: Use GitHub Discussions for questions
- **Email**: Contact maintainers at email@example.com

## 📄 License

By contributing, you agree that your contributions will be licensed under the same license as the project (MIT License).

---

Thank you for contributing to AI Agents Orchestrator! 🚀
