# CLAUDE.md — Orquestrador de Agentes IA

Orquestrador que monta Agents/Teams a partir de configuração (MongoDB; YAML a partir da F2) e os expõe por HTTP/AG-UI/MCP. Runtime atual: **Agno** (AgentOS) sobre FastAPI. Objetivo: plug-and-play e agnóstico a fornecedores (LLM, embedder, vector store, config store, tools). O plano de trabalho está em `docs/roadmap/`.

## Arquitetura (onion / hexagonal)
```
src/domain          entidades e portas (Protocol/ABC). Sem dependência externa.
src/application     casos de uso e serviços; importa só domain.
src/infrastructure  adapters: Mongo, HTTP, Agno, OTel, config, web. Implementa as portas.
src/presentation    controllers/rotas.
```
- **Regra de dependência:** `domain` ← `application` ← `infrastructure`/`presentation`. Imposta por `lint-imports` (contratos no `pyproject.toml`). Violações herdadas estão listadas como baseline em `ignore_imports`: **não copie o padrão e não aumente a lista**; ela só encolhe.
- `agno` só em `src/infrastructure/runtime/agno/` ao fim da F2 (até lá, só onde já existe).
- **Anti over-engineering:** porta, registry ou plugin só com 2+ implementações reais e teste de contrato. Uma implementação só ⇒ módulo simples. Entry points apenas onde plugin de terceiros é plausível.
- Composition root: `src/infrastructure/dependency_injection.py`. Nada de service locator.

## Convenções
- Docstrings, logs e mensagens em **português**; identificadores em **inglês** (exceto campos do Mongo já existentes, como `nome`, `descricao`, que são imutáveis).
- Tipagem em tudo que é público; sem `Any` em portas e entidades.
- Sem I/O síncrono bloqueante em caminho async (use `asyncio.to_thread` ou cliente async).
- Sem erro engolido: `except` sempre loga com contexto ou re-levanta. Nunca devolva `str(exc)` ao cliente HTTP.
- Segredos só por referência (`env:VAR`, `file:/caminho`); nunca em código, log, teste, trace ou documento Mongo. Não leia `.env`.
- Documentos Mongo existentes continuam válidos: campo novo é opcional com default.
- APIs de terceiros: confira no pacote instalado (`.venv/lib/python3.12/site-packages/...`) ou na doc da versão fixada. Nunca de memória.
- Linhas citadas no roadmap são indicativas: reconfirme com `git grep` antes de mexer.

## Testes
- Pirâmide: `unit` → `contract` (mesma suíte para toda implementação de uma porta) → `integration` → `security` → `eval` (modelo scriptado). `live` = depende de serviço externo real; nunca roda na CI padrão.
- **Nenhum LLM real nem rede externa** em testes. Use os fakes de `tests/fakes/`.
- Teste de comportamento: asserte o resultado observável. `assert x is not None` sozinho não é teste; `patch` de detalhe interno só quando não houver fake.
- Todo defeito começa com um teste vermelho que o reproduz.

## Comandos (venv do projeto)
```bash
.venv/bin/python -m pytest -m "not live"            # suíte
.venv/bin/python -m pytest -m unit --cov            # unit com cobertura
.venv/bin/ruff check src tests app.py
.venv/bin/mypy
.venv/bin/lint-imports
.venv/bin/bandit -q -c pyproject.toml -r src
.venv/bin/pip-audit -r requirements.lock --require-hashes --disable-pip
```
Dependências: `requirements.in`/`requirements-dev.in` → locks com hash (`pip-compile`, ver CONTRIBUTING). Nunca edite o lock à mão.

## Gates por fase (bloqueantes no gate final do /dev-cycle)
| Fase | Gates |
|---|---|
| F0 | `ruff check`, `pytest -m "not live"` com cobertura, `mypy` (módulos novos), `lint-imports` (com baseline) |
| F1 | F0 + `bandit` (sem achado novo; zera no F1-03), `pip-audit` (só os conhecidos: agno PYSEC-2026-2333 até a F3; chromadb até o F1-02), `pytest -m security` |
| F2 | F1 + testes de contrato, `lint-imports` sem baseline em `domain`/`application`, cobertura do diff ≥ 85% |
| F3+ | F2 + matriz de versões quando aplicável, evals determinísticas |

## Definition of Done
Critérios de aceite do item cobertos por teste passando; gates da fase verdes; docs afetadas atualizadas (README, `.env.example`, roadmap); HANDOFF do dev, review APPROVE e QA PASS registrados no ciclo.

## Cadeia de desenvolvimento
`/dev-cycle <item> [--commit]` orquestra `python-dev-specialist` → `security-architecture-reviewer` → `qa-engineer` (`.claude/agents/`). O item em `docs/roadmap/` é a fonte da verdade de escopo. Push e PR nunca acontecem dentro do ciclo.

`.github/instructions/codacy.instructions.md` (local, ignorado pelo git) vale para o Copilot/MCP do Codacy, **não** para esta cadeia; a análise do Codacy roda na CI.
