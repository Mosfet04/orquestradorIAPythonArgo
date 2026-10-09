# F2 — Núcleo plugável

Objetivo: adicionar provedor de LLM/embedder, backend de config ou runtime **sem editar `domain`/`application`**, com o Agno confinado a `src/infrastructure/runtime/agno/`. Itens detalhados ao começar a fase (o dev do primeiro item confirma o desenho abaixo contra o código da época).

Princípios: porta só com 2+ implementações reais e teste de contrato; built-ins registrados em código no mesmo registry dos plugins; entry points só em `orquestrador.providers` e `orquestrador.config_stores` nesta fase; allowlist por metadados **antes** de `ep.load()`; id duplicado = erro; `module:attr` vindo de config só com `ALLOW_DYNAMIC_IMPORT=true` em loopback + development/test.

## F2-01 — Registry e loader seguro de plugins
## F2-02 — Entidades neutras (`ModelConfig`, `AgentSpec`, `TeamSpec`) + mappers legados do Mongo
Campos opcionais novos: `model_params`, `base_url`, `api_key_ref`.
## F2-03 — `SecretResolver` (env, file) + `ProviderRegistry` com os built-ins + `openai_compatible`
## F2-04 — Embeddings via providers; remoção do `EmbedderModelFactory` duplicado
## F2-05 — `AgnoRuntime`: montagem de agents/teams; `application` e controller deixam de importar Agno
## F2-06 — `AgnoRuntime`: ciclo de vida do AgentOS (`db=`, lifespans que rodam — B15), router AG-UI
O agno 2.5.8 usa `MongoDb` síncrono dentro de `arun` (I/O bloqueante no event loop em toda leitura/gravação de sessão e memória): avaliar `AsyncMongoDb` do agno ao passar `db=` (achado da revisão do F1-08).
## F2-07 — `ConfigStore`: Mongo + YAML
Pendência herdada da F0-03: a suíte de `tests/contract/` hoje roda só contra os repositórios em memória; neste item ela passa a rodar também contra o adapter Mongo (marker `integration`/`contract` com Mongo real ou efêmero) e o contrato define (a) a semântica de id duplicado (erro, sobrescrita ou ignorado) e (b) a ordem entre lotes de `save_nodes` (chamadas sucessivas preservam ou não a ordem de inserção na leitura).
## F2-08 — `lint-imports` sem baseline em `domain`/`application` e reescrita dos testes acoplados ao Agno
