# Contributing to AI Agents Orchestrator

Thank you for your interest in contributing to the AI Agents Orchestrator! This document provides guidelines and information for contributors.

## Table of Contents

- [Code of Conduct](#code-of-conduct)
- [Getting Started](#getting-started)
- [Development Setup](#development-setup)
- [Contributing Guidelines](#contributing-guidelines)
- [Code Standards](#code-standards)
- [Testing](#testing)
- [Documentation](#documentation)
- [Pull Request Process](#pull-request-process)
- [Issue Reporting](#issue-reporting)

## Code of Conduct

This project adheres to a code of conduct. By participating, you are expected to uphold this code. Please report unacceptable behavior to the project maintainers.

### Our Pledge

- Be respectful and inclusive
- Welcome newcomers and help them learn
- Focus on what is best for the community
- Show empathy towards other community members

## Getting Started

### Prerequisites

- Python 3.9 or higher
- MongoDB 4.4 or higher
- Git
- Basic understanding of Clean Architecture principles

### Quick Setup

1. **Fork the repository**
   ```bash
    git clone https://github.com/Mosfet04/orquestradorIAPythonArgo.git
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

## Development Setup

### Manual Setup

1. **Virtual Environment**
   ```bash
   python -m venv venv
   source venv/bin/activate  # Linux/macOS
   # or
   .\venv\Scripts\Activate.ps1  # Windows
   ```

2. **Install Dependencies** (pinned, hash-checked; see [Dependencies and lock files](#dependencies-and-lock-files))
   ```bash
   pip install --require-hashes -r requirements.lock -r requirements-dev.lock
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

### Dependencies and lock files

| File | Edited by | Role |
|---|---|---|
| `requirements.in` | hand | Direct runtime dependencies (`agno==2.5.8` exact, others with a minimum version) |
| `requirements.lock` | `pip-compile` | Every runtime package pinned with `==` and `--hash` |
| `requirements-dev.in` | hand | Tests and tooling; constrained by `-c requirements.lock` |
| `requirements-dev.lock` | `pip-compile` | Every dev package pinned with `==` and `--hash` |
| `requirements.txt` | hand | Compatibility shim: `-r requirements.lock` |

Never edit a `.lock` file by hand. Use the `pip-tools` installed by the dev lock, always runtime first (the dev lock is constrained by it):

```bash
# Update everything (the normal way to refresh the locks), runtime first
.venv/bin/pip-compile --upgrade --generate-hashes --allow-unsafe --strip-extras \
    --output-file=requirements.lock requirements.in
.venv/bin/pip-compile --upgrade --generate-hashes --allow-unsafe --strip-extras \
    --output-file=requirements-dev.lock requirements-dev.in

# Or upgrade a single package (repeat on the dev lock if the package is shared)
.venv/bin/pip-compile --upgrade-package fastapi --generate-hashes --allow-unsafe --strip-extras \
    --output-file=requirements.lock requirements.in

# ALWAYS audit the result and check that both locks install
.venv/bin/pip-audit -r requirements.lock --require-hashes --disable-pip
pip install --dry-run --require-hashes -r requirements.lock -r requirements-dev.lock
```

Without `--upgrade`/`--upgrade-package`, `pip-compile` keeps the current pins, so it never
fixes a vulnerable version by itself. Every remaining `pip-audit` finding must be
justified in the PR description (why it does not apply or why it cannot be fixed yet).

Notes:
- Locks are generated on **Linux / CPython 3.12** and validated for Linux CPython 3.11 and 3.12 (Docker image and CI). `pip-compile` resolves for the machine it runs on: Windows-only transitive dependencies are dropped. That is why `colorama` is listed explicitly in `requirements.in`. On native Windows, if `--require-hashes` still fails, use WSL/Docker or regenerate locally (and do not commit that lock).
- `uvloop` is declared with `sys_platform != "win32"`. Note: `app.py` still imports `uvloop` unconditionally, so it fails on native Windows until roadmap item F1-03.
- Optional extras are **not installed by default** and stay out of the lock: `PyJWT` (AgentOS JWT auth), `mcp` (MCP tools), `anthropic`, `groq` (model providers). To adopt one, add it to `requirements.in` with a minimum version and regenerate.
- A new dependency needs a justification in the PR (license, maintenance, discarded alternatives).
- Upper bounds in `requirements.in` are deliberate and explained next to each one: `fastapi<0.137` (agno 2.5.8 breaks with the `_IncludedRouter` introduced in 0.137), major caps on SDKs consumed by agno (`openai<3`, `google-genai<2`, `ag-ui-protocol<0.2`, `openinference-instrumentation-agno<0.2`) until the agno migration (F3). Do not lift them in a routine `--upgrade`.

### Using Docker

Credentials come only from `.env` (see `.env.example`); required variables use `${VAR:?}` and compose refuses to start without them. The base file needs only `MONGO_CONNECTION_STRING` (still required with the dev override); the dev override also needs `MONGO_ROOT_*`/`MONGO_EXPRESS_*` (URL-safe values).

```bash
# App only (MongoDB/Ollama external, from .env)
docker compose up -d

# Development: app + local MongoDB, Ollama and mongo-express (ports bound to 127.0.0.1)
docker compose -f docker-compose.yml -f docker-compose.dev.yml up -d

# View logs
docker compose logs -f app

# Stop services
docker compose -f docker-compose.yml -f docker-compose.dev.yml down
```

`.env.development` is versioned and holds placeholders only (`CHANGE_ME`); never put a real secret in it.

## Contributing Guidelines

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

## Code Standards

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

All tool configuration lives in `pyproject.toml`. Run before committing:
```bash
.venv/bin/ruff check src tests app.py  # lint (incl. import sorting); `ruff format` is not adopted yet
.venv/bin/mypy                          # strict by default; legacy modules listed with ignore_errors
.venv/bin/lint-imports                  # onion layer contracts
.venv/bin/python -m pytest --cov        # tests + coverage (source = src, branch = true)
```

The legacy findings are recorded as an explicit baseline (`per-file-ignores` in ruff,
`ignore_errors` modules in mypy, `ignore_imports` in import-linter). **The baseline can only
shrink**: fix and remove an entry when you touch that code; never add new entries. New modules
are checked with every rule.

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

## Testing

### Testing Strategy

We use **pytest** with the following test types:

```
tests/
├── unit/           # Fast, isolated tests                       -> marker `unit`
├── golden/         # Snapshots of the kwargs passed to agno Agent/Team -> marker `unit`
├── contract/       # Same suite for every implementation of a port -> marker `contract`
├── integration/    # Tests across layers                        -> marker `integration`
└── fakes/          # FakeChatModel, FakeEmbedder, in-memory repositories, RecordingLogger
```

- Layer markers (`unit`, `contract`, `integration`, `security`, `eval`) are applied **by directory**
  in `tests/conftest.py`; do not decorate files with them. A test in an unmapped directory fails
  collection. `live` (needs a real external service) is set on the test itself and never runs in CI.
- No real LLM, network or MongoDB in tests: use `tests/fakes/` (`FakeChatModel` is an
  `agno.models.base.Model`, so it can be passed straight to `Agent`/`Team`).
- Tests that configure OpenTelemetry providers use the `reset_otel_providers` fixture.
- **Random order** (`pytest-randomly`): every run shuffles the tests and prints
  `Using --randomly-seed=N`. Reproduce a failure with `-p randomly --randomly-seed=N`;
  use `-p no:randomly` for the file order.
- **Golden tests**: if a kwarg passed to `Agent`/`Team` changes, `tests/golden` fails with a diff.
  When the change is intentional, regenerate and review the JSON diff in the commit:
  `pytest tests/golden --update-golden`.

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

# no @pytest.mark.integration needed: the directory sets the marker
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

# Default CI run (everything but `live`)
pytest -m "not live"

# One layer only
pytest -m unit
pytest -m contract
pytest -m integration

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

## Documentation

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

## Pull Request Process

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

1. **Automated checks** must pass (CI/CD, see below)
2. **Code review** by at least one maintainer
3. **Manual testing** for significant changes
4. **Documentation review** if docs are updated

### Continuous integration

`.github/workflows/ci.yml` runs on every push to the tracked branches and on every PR:

| Job | Runs | Blocking |
|---|---|---|
| `lint` (py3.12) | `ruff check src tests app.py`, `mypy`, `lint-imports` | yes |
| `test` (py3.11, py3.12) | `pytest -m "not live"` with coverage | yes |
| `security` (py3.12) | `bandit -c pyproject.toml -r src`, `pip-audit -r requirements.lock --require-hashes --disable-pip` | not yet: `continue-on-error` until phase F1 |
| `codacy-coverage` | uploads the py3.12 `coverage.xml` to Codacy | push or same-repo PR only |

Rules (checked by `tests/unit/test_ci_workflow.py`): triggers only `push` and `pull_request`
(no `pull_request_target`/`workflow_run`); `permissions: contents: read` at the top;
every `uses:` pinned to a 40-hex commit SHA with a `# vX.Y.Z` comment; `persist-credentials: false`
on checkout; dependencies installed only from the hash-checked locks; no `curl | bash`.
`CODACY_API_TOKEN` exists only in the env of the upload step, in a job that never checks out
or installs the repository code. To bump an action, resolve the tag to its commit
(`git ls-remote https://github.com/<owner>/<repo> refs/tags/<tag>`) and update SHA and comment.
To bump the Codacy reporter, change `REPORTER_VERSION` and `REPORTER_SHA256` together (check the
hash against the release's `.SHA512SUM`/asset digest). The upload uses `--prefix src/` because
`[tool.coverage.run] source = ["src"]` makes `coverage.xml` list paths relative to `src/`, and the
upload job has no `.git` for the reporter to match them; keep both in sync if `source` changes. Validate locally with
[`actionlint`](https://github.com/rhysd/actionlint) and [`zizmor`](https://docs.zizmor.sh/)
(`pipx run zizmor --offline .github/workflows/`); neither is part of the dev lock.

## Issue Reporting

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

## Recognition

Contributors will be:

- **Listed in CONTRIBUTORS.md**
- **Mentioned in release notes**
- **Given credit in documentation**

## Getting Help

- **Discord**: [Join our Discord server](discord-link)
- **Discussions**: Use GitHub Discussions for questions
- **Email**: Contact maintainers at <email@example.com>

## License

By contributing, you agree that your contributions will be licensed under the same license as the project (MIT License).

---

Thank you for contributing to AI Agents Orchestrator! 🚀
