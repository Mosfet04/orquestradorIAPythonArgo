# F0 — Fundação

Objetivo da fase: ferramental e rede de segurança de testes **antes** de mexer em comportamento. Nenhuma mudança funcional no produto.

## F0-01 — pyproject, lock e ferramentas de qualidade
Objetivo: um único lugar de configuração de ferramentas e dependências reprodutíveis.
Escopo:
- `pyproject.toml` com configuração de `ruff` (lint; sem `format --check` nesta fase), `mypy` (estrito só para módulos novos via overrides; legado com `ignore_errors` listado), `pytest` (migrar `pytest.ini` para `[tool.pytest.ini_options]` e apagar o `pytest.ini`), `coverage` (`source = ["src"]`, `branch = true`), `import-linter` (contratos de camadas `domain` ← `application` ← `infrastructure`/`presentation`, com as violações atuais em `ignore_imports` como baseline) e `bandit` (`[tool.bandit]`).
- `requirements.in` (dependências de runtime diretas, com `agno==2.5.8` exato) e `requirements.lock` gerado com hashes (`pip-compile --generate-hashes` ou equivalente). `requirements-dev.in`/`requirements-dev.lock` com pytest, pytest-asyncio, pytest-cov, ruff, mypy, import-linter, bandit, pip-audit, diff-cover, respx, pip-tools.
- `requirements.txt` passa a apontar para o lock (ou é removido e README/Dockerfile/CI/setup.sh atualizados para o lock).
- Extras opcionais documentados (sem instalar por padrão): `PyJWT`, `mcp`, `anthropic`, `groq`.
- `uvloop` explícito só fora do Windows (marker `sys_platform != "win32"`).
- Corrigir o que o `ruff check` acusar **somente** se for trivial e mecânico (imports não usados etc.); o resto vai para `per-file-ignores`/baseline documentado.
Fora de escopo: reformatar o código (`ruff format`), mudar comportamento, CI.
Critérios de aceite:
- `pip install --require-hashes -r requirements.lock -r requirements-dev.lock` em venv limpo funciona (pode ser verificado com `pip install --dry-run --require-hashes`).
- `ruff check src tests`, `mypy`, `lint-imports` e `pytest --cov` passam; a cobertura reportada respeita `source`/`omit`.
- `pytest.ini` não existe mais e os markers declarados continuam válidos (`--strict-markers`).
Testes exigidos: a suíte existente continua verde.
Riscos: lock gerado em Linux/py3.12; documentar como regenerar.

## F0-02 — CI mínimo e seguro
Objetivo: a CI roda os gates e não vaza segredo.
Escopo: workflow com jobs `lint` (ruff, mypy, lint-imports), `test` (matriz py3.11/3.12, `pytest -m "not live"` com cobertura), `security` (bandit, pip-audit). `permissions: contents: read` no topo; actions fixadas por SHA com comentário da versão; `CODACY_API_TOKEN` só no passo que envia cobertura; sem `curl | bash` (baixar o reporter com checksum ou usar a action oficial fixada por SHA). Instalação pelo lock com hashes.
Fora de escopo: integração com Mongo real (entra quando houver testes `integration` que precisem).
Critérios de aceite: `actionlint` e `zizmor` (se instaláveis) limpos; o token não aparece em nenhum passo de instalação (checado no YAML); o workflow referencia apenas o lock.
Testes exigidos: validação estática do YAML (actionlint/zizmor) ou, na falta deles, teste em `tests/unit` que carrega o YAML e verifica `permissions`, SHAs e escopo do segredo.

## F0-03 — Fakes, markers e golden tests
Objetivo: rede de segurança para a refatoração da F2.
Escopo:
- `tests/fakes/`: `FakeChatModel` scriptado (respostas pré-definidas, registra chamadas) e `FakeEmbedder` determinístico; repositórios em memória para as portas de `domain/repositories`.
- Markers `unit/contract/integration/security/eval/live` declarados; testes existentes marcados `unit` (por `conftest.py` de diretório, não arquivo a arquivo).
- `conftest.py` sem estado global vazando: reset do tracer/meter provider do OTel entre testes que os configuram; `setup_logging()` não roda mais no import de `src/infrastructure/logging/config.py` (chamado explicitamente pelo `app.py`).
- `tests/golden/`: snapshot dos kwargs que `AgentFactoryService` e `TeamFactoryService` passam a `Agent`/`Team` em 4 configurações (mínima, com tools, RAG semantic, RAG hierarchical), salvos em JSON versionado. Atualização só com `--update-golden` explícito.
Critérios de aceite: `pytest -m unit` e `pytest -m contract` rodam isolados; a suíte passa em ordem aleatória (`-p random_order` ou `pytest-randomly`, se adotado, ou duas ordens fixas); golden falha se um kwarg mudar sem atualização explícita.
Testes exigidos: os próprios golden; teste que prova que importar `src.infrastructure.logging.config` não configura logging.
