# F3 — Migração Agno 2.5.8 → 3.1.12

Contexto e resíduos herdados: seção "F3" de [F3-F9.md](F3-F9.md). Versão alvo: **agno 3.1.12** (última da linha 3.1).

Estratégia: preparar no 2.5.8 tudo que dá para deixar neutro à versão (F3-02..F3-04, suíte verde pela CI normal), depois um bump atômico (F3-05), e só então esquema, runbook e resíduos. Sem job de CI de matriz 2.5.8 × 3.x: o CI instala por lock com hash e um segundo lock seria superfície de supply chain; o mypy pega drift de assinatura em override e o F3-05 faz um dual-run local do conjunto neutro (receita no F3-01).

Decisões do dono (2026-11-15):
- `reasoning`: no 3.x, `AgentConfig.reasoning=true` vira `ReasoningTools(add_instructions=True)`; documentar que não há mais fase separada nem `reasoning_max_steps` (F3-05).
- Telemetria dos evals: router próprio responde 404 em `/eval-runs*` até a F8 (F3-08).
- Contratos que mudam no 3.x (histórico do AG-UI vindo da sessão, `background=true` sem `stream=false` vira SSE, sessão de outro usuário ⇒ 404, `GET /models` some, chave `memory` de `GET /agents/{id}`): aceitar e documentar no runbook (F3-07).

Quebras levantadas no spike (fonte do 3.1.12; linhas indicativas, reconfirme no pacote instalado):

| Uso nosso | Mudança no 3.1.12 | Evidência (agno 3.1.12) |
|---|---|---|
| `Agent(reasoning=)` | removido com `reasoning_min/max_steps`; `__init__` fechado ⇒ `TypeError` | `agent/agent.py:468-469`; `tools/reasoning.py:10` |
| `enable_user_memories` | removido; `getattr(...) is True` vira False em silêncio e desliga o guardrail de `user_id` | `agent/agent.py:437` |
| `MongoDb.prepare_doc` (override) | novo kwarg `user_id`; o agno engole o `TypeError` e o RAG sobe vazio | `vectordb/mongodb/mongodb.py:623,1097` |
| `_create_search_index` (override) | índice declara filtro `user_id` e o `search` filtra por ele | `vectordb/mongodb/mongodb.py:333-338,733` |
| `AsyncMongoDb.get_session` (override) | sem filtro de `session_type`; `runs_limit` sempre passado; runs em `agno_runs` | `db/mongo/async_mongo.py:899-967`; `agent/_storage.py:392` |
| `get_latest_schema_version` / `MigrationManager` | implementados para Mongo (resolve o 207); cleanup é `cleanup_legacy_runs_field` | `async_mongo.py:563`; `db/mongo/mongo.py:372` |
| AG-UI | helpers saem de `agui.utils` para `agui/stream.py`/`agui/input.py`; histórico vem da sessão; evento não mapeado vira `RAW` | `agui/router.py:73-80`; `handlers.py:338-343` |
| Rotas do AgentOS | 109 → 152; novas `/queue*`, `/service-accounts*`, `/filesystem*`, `/learnings*`; `GET /models` some | `os/app.py:2291` |
| Rota de run | sessão de outro dono ⇒ 404; `background=true` com stream default ⇒ SSE | `os/middleware/user_scope.py:494-545` |
| Telemetria de evals | `/eval-runs` segue com `telemetry=True` ignorando `AGNO_TELEMETRY` | `os/routers/evals/utils.py:27`; `eval/utils.py:198` |
| Cancelamento | `BaseRunCancellationManager` ganha `register_member_run` (no-op); xfail F110 viram XPASS | `run/cancellation_management/base.py:90` |
| Embedder | erro levanta `EmbeddingError` em vez de `[]` | `knowledge/embedder/ollama.py:110,146` |
| Dependências | faltam `python-multipart` e `sqlalchemy` explícitos; PYSEC-2026-2333 corrigido (afeta ≤ 2.6.5) | `os/app.py:1221` |

## F3-01 — Refresh dos locks no 2.5.8
Modelo: sonnet
Objetivo: isolar variáveis: o lock do bump só deve carregar o que o agno muda. Hoje o `pip-audit` já falha com achados sem relação com o agno (pyasn1, pypdf, dnspython, opentelemetry-api).
Escopo: `pip-compile --upgrade` completo de `requirements.lock` e `requirements-dev.lock`, agno fixo em 2.5.8 e tetos atuais mantidos (`fastapi<0.137` etc.). Receita no CONTRIBUTING para montar a venv 3.x temporária de dual-run (`--upgrade-package agno` num arquivo temporário + `pip install --require-hashes`, fora do `.venv`). `.gitignore` com `.env.*` (exceto os versionados).
Fora de escopo: qualquer código de produto; subir o agno; subir tetos.
Critérios de aceite: `pip-audit -r requirements.lock --require-hashes --disable-pip --ignore-vuln PYSEC-2026-2333` limpo; ruff, mypy, lint-imports, bandit e `pytest -m "not live" -n auto` verdes; `pip install --dry-run --require-hashes -r requirements.lock -r requirements-dev.lock` ok.
Testes exigidos: suíte completa (sem teste novo: o item não muda comportamento).
Riscos e dependências: pypdf 6→7, starlette, pydantic e pymongo sobem (o `src` não importa pypdf diretamente); lock gerado em 3.12 e validado em 3.11 pela CI.

## F3-02 — Preparação no 2.5.8: memória por usuário e fakes
Modelo: opus
Objetivo: tirar o renome de memória do bump e dar ao fake de Mongo as consultas que o 3.x usa.
Escopo: (a) factories passam `update_memory_on_run` (canônico já no 2.5.8) e o golden é regenerado; (b) o guardrail de `user_id` lê o atributo canônico tipado, sem `getattr` com default; (c) `FakeMongoCollection` ganha `$addFields`, `$ifNull` e `$nin`, com caso no contrato de fidelidade (também no `mongo-live`); (d) testes que leem `doc["runs"]` passam à API pública (`get_session` ou `GET /sessions/{id}/runs`).
Fora de escopo: `reasoning`; `SessionDb`.
Critérios de aceite: teste vermelho antes provando que Agent e Team com memória (membros incluídos) exigem `user_id` sem depender de `enable_user_memories`; golden só muda nesses kwargs; suíte verde no 2.5.8.
Testes exigidos: unit do guardrail, golden, contrato de fidelidade do fake.
Riscos e dependências: baixo; depende do F3-01.

## F3-03 — Preparação no 2.5.8: RAG semântico
Modelo: opus
Objetivo: `GuardedMongoVectorDb` e o índice funcionam nos dois agnos.
Escopo: `prepare_doc(..., **kwargs)` repassando ao super; `_create_search_index` declara o filtro `user_id`; `repair()` recria índice legado sem o filtro; logs de erro de embedder só com o tipo da exceção.
Fora de escopo: porta VectorStore e espera `queryable` (F7).
Critérios de aceite: índice novo e reparado trazem o campo `user_id`; testes do F2-11 seguem verdes; mypy sem override incompatível.
Testes exigidos: unit com o fake de vector store; `live` (Atlas Local, `MONGO_LIVE_URL`) com `$vectorSearch` + filtro `user_id` e reparo de índice legado.
Riscos e dependências: recriação do índice Atlas uma vez no startup (remoção assíncrona). Só o `live` prova o filtro: rodá-lo localmente antes do merge.

## F3-04 — Preparação no 2.5.8: borda, AG-UI e telemetria
Modelo: opus
Objetivo: deixar neutro à versão o que a borda e o AG-UI precisam saber do 3.x.
Escopo: (a) `classify_route` classifica como admin `/queue*`, `/service-accounts*`, `/filesystem*`, `/learnings*` (inexistentes no 2.5.8, sem efeito) e revisa as novas rotas RUN (`fork`, `compact`, `checkpoints`, `resume`); (b) AG-UI com conversão própria: só a última mensagem do usuário + `forwardedProps.user_id`, sem client tools/contexto/mídia, e filtro que descarta `RAW` e `CUSTOM` não previstos; (c) testes por invariante: sessão de outro `user_id` nunca lida nem sobrescrita, `background=true` com `stream=false`, controle positivo de telemetria que drena o envio em background.
Fora de escopo: `assert_session_writable` no AG-UI e identidade do principal (F5/F6).
Critérios de aceite: a tabela de classificação cobre todas as rotas das duas versões; AG-UI emite só tipos de evento da lista; suíte verde no 2.5.8.
Testes exigidos: unit de `classify_route` e do filtro de eventos; integração/segurança ajustadas.
Riscos e dependências: muda o histórico do AG-UI já no 2.5.8 (contrato novo, ver decisões).

## F3-05 — Bump atômico para o agno 3.1.12
Modelo: opus
Objetivo: pin, lock e adaptação mínima, com a suíte verde no 3.1.12.
Escopo: `agno==3.1.12`, `python-multipart` e `sqlalchemy` no `requirements.in`; locks por `--upgrade-package agno`; remover `--ignore-vuln PYSEC-2026-2333` do ci.yml, CLAUDE.md e CONTRIBUTING. `reasoning=true` mapeado para `ReasoningTools(add_instructions=True)` (decisão do dono). `SessionDb` sem o override de `get_session`. Imports do AG-UI (`stream`, `input`). Snapshots de rotas/middleware regenerados com revisão rota a rota. Remover os 2 xfail do F1-10 (anotar no F9). Ajustar contratos (chave `memory`, `/models`, `/knowledge` 503, runs em `agno_runs`). Ids reservados do agno viram recusa clara na validação.
Fora de escopo: esquema do banco; telemetria dos evals; motor.
Critérios de aceite: ruff, mypy, lint-imports, bandit e suíte verdes no 3.1.12; `pip-audit` sem ignore; diferenças de comportamento listadas no item.
Testes exigidos: suíte completa; dual-run local do conjunto neutro nas duas venvs.
Riscos e dependências: depende de F3-01..F3-04; maior item (excluindo lock e snapshots).
Pendências herdadas do F3-02: `tests/integration/test_qa_f2_04_runtime_startup.py` asserta `memory.update_memory_on_run` em `GET /agents/{id}` (some no 3.x); contagens de runs via `stored_runs` em testes de Team podem incluir sub-runs de membro (`parent_run_id`) no 3.x.

## F3-06 — Esquema do banco e `MigrationManager`
Modelo: opus
Objetivo: migração explícita, verificável e reversível das sessões.
Escopo: módulo de migração com `python -m` que roda `MigrationManager(db).up()` sobre o `SessionDb` e imprime a verificação (sessões com `runs` inline × documentos em `agno_runs`). A v3.0.0 copia `sessions.runs` para `agno_runs` (upsert por `run_id`) sem destruir. Nunca chama `cleanup_legacy_runs_field`. `/databases/{id}/migrate` responde 200 (fim do 207).
Fora de escopo: cleanup do campo legado (release posterior, manual).
Critérios de aceite: migrar duas vezes dá o mesmo resultado; sessão não migrada continua legível; teste prova que o módulo não referencia o cleanup; rollback por `down` testado.
Testes exigidos: unit; `live` com fixture de sessão gerada no 2.5.8.
Riscos e dependências: depois que o 3.x grava, sessões tocadas perdem o `runs` inline e um rollback para o 2.5.8 deixa de ver esse histórico (só restore do backup).

## F3-07 — Runbook e docs
Modelo: sonnet
Objetivo: operação segura da virada.
Escopo: `docs/runbooks/f3-agno-3.md` (pré-checagens, `mongodump` completo, ordem backup → deploy → migrate → verificar → liberar, rollback por restore ou `down`, "nunca o cleanup no mesmo release", mudanças de contrato REST/AG-UI). README pt-br/en, CONTRIBUTING, CLAUDE.md (gates sem ignore) e status do roadmap.
Fora de escopo: código.
Critérios de aceite: comandos conferidos contra o F3-06; links válidos; docs citam 3.1.12.
Testes exigidos: testes de docs existentes (ex.: `tests/unit/test_env_files.py`).
Riscos e dependências: depende do F3-06.

## F3-08 — Telemetria dos evals (`/eval-runs`)
Modelo: opus
Objetivo: fechar o resíduo do F1-03 conforme a decisão do dono.
Escopo: router próprio que responde 404 em `/eval-runs*` (padrão `preserve_base_app` do cancelamento) até a F8; atualizar `test_agno_telemetry.py` e o roadmap.
Fora de escopo: evals determinísticos (F8).
Critérios de aceite: com `AGNO_TELEMETRY=false`, nenhuma tentativa de egress para a telemetria do agno a partir de `/eval-runs` (bloqueador de rede no teste).
Testes exigidos: segurança com bloqueador de rede e drenagem do envio em background.
Riscos e dependências: depende do F3-05.

## F3-09 — SessionDb em `AsyncMongoClient`
Modelo: opus
Objetivo: sair do motor (fim de vida) onde o agno 3.x já suporta o driver nativo.
Escopo: `SessionDb` usa o `AsyncMongoClient` do pymongo com `close` aguardado; depois consolidar `MongoClientFactory` e o container, trocando o `FakeMotorClient` por um fake do cliente nativo. Passou de ~600 linhas ⇒ dividir em F3-09b (repositórios).
Fora de escopo: remover a dependência `motor` antes de zerar o uso.
Critérios de aceite: `trap_sync_pymongo` não vê I/O síncrono no loop; sessão e memória idênticas; `live` com Atlas Local verde.
Testes exigidos: unit, integração e `live`.
Riscos e dependências: só o `live` prova `aggregate`/`to_list` no driver nativo.
