# F2 — Núcleo plugável

Objetivo: adicionar provedor de LLM/embedder ou backend de config **sem editar `domain`/`application`**, com o Agno confinado a `src/infrastructure/runtime/agno/`. Ao fim da fase, `lint-imports` roda sem baseline em `domain`/`application`.

Princípios (valem para todos os itens):
- Porta só com 2+ implementações reais e teste de contrato; senão é módulo simples. Nada de registry genérico "para o futuro".
- Built-ins registrados em código, no mesmo registry dos plugins externos (sem caminho privilegiado).
- Plugins externos só por entry point (`orquestrador.providers`), com allowlist verificada **antes** de `ep.load()` (metadados: distribuição + valor); id duplicado = erro. Import `module:attr` vindo de configuração só com `ALLOW_DYNAMIC_IMPORT=true` e somente no modo dev local (loopback + development/test), como a regra de auth do F1-04.
- Documentos Mongo existentes continuam válidos: campos novos são opcionais; o mapper legado traduz.
- Os golden tests (`tests/golden/`) são a rede de segurança: kwargs de `Agent`/`Team` só mudam com justificativa explícita no HANDOFF.

## F2-01 — `ModelConfig` neutro e segredos por referência
Escopo: entidade de domínio `ModelConfig(provider, model_id, params, base_url, api_key_ref)` (imutável, validada, sem tipos de terceiros); `AgentConfig`/`TeamConfig`/`RagConfig` passam a expor `ModelConfig` (mapper legado: `factory_ia_model`+`model` → `ModelConfig`; campos novos opcionais `model_params`, `base_url`, `api_key_ref` nos documentos). Resolução de segredo por referência (`env:VAR`, `file:/caminho` — arquivo confinado e só leitura; valor nunca logado/repr) como função simples em `infrastructure` (sem porta: uma função com despacho por esquema). Sem mudar ainda como o modelo é criado.
Critérios: documentos antigos geram o mesmo `ModelConfig` de antes; `api_key_ref` inválido/ausente ⇒ erro claro sem valor; golden inalterado.

## F2-02 — `ProviderRegistry` (modelos e embedders) + `openai_compatible`
Escopo: registry de providers em `infrastructure` (spec por provider: id, aliases, caminho pontilhado da classe Agno de chat e de embedder, pacote do SDK, mapeamento de kwargs) substitui `ModelFactory` e `EmbedderModelFactory` duplicados; built-ins: ollama, openai, anthropic, gemini, groq, azure e **openai_compatible** (`base_url` + `api_key_ref`, via `OpenAILike` e embedder compatível do Agno — conferir no 2.5.8); `model_params` repassados com allowlist de chaves por provider. As portas `IModelFactory`/`IEmbedderFactory` param de devolver `Any` onde der (tipo opaco de domínio).
Critérios: matriz de providers (instancia ou falha nomeando o pacote), inclusive `openai_compatible`; adicionar provider = só registrar uma spec (teste com spec fake); golden só muda se justificado.

## F2-03 — Plugins de provider por entry point (allowlist antes do load)
Escopo: loader de `orquestrador.providers` com allowlist por env (`PLUGIN_ALLOWLIST`, formato `distribuição:nome`), verificação sobre metadados antes de `ep.load()`, id duplicado ou conflito com built-in = erro de startup; `module:attr` por config só no modo dev local com `ALLOW_DYNAMIC_IMPORT=true`. Documentar como escrever um plugin (README + exemplo mínimo em `examples/`, sem dependência nova).
Critérios: plugin fora da allowlist nunca é importado (prova: módulo com efeito colateral não executa); duplicata recusa o startup com mensagem clara; plugin permitido registra e é usável.

## F2-04 — `AgnoRuntime` (1/2): montagem de agents/teams fora de `application`
Escopo: `AgentFactoryService`, `TeamFactoryService`, `hierarchical_search_tool`, guardrail de `user_id` e o que mais importar `agno` em `application` vão para `src/infrastructure/runtime/agno/`; `application` passa a depender de uma porta `AgentRuntime` (`build_agents(configs)`, `build_teams(configs, agents)`) que devolve handles opacos; use cases e controller deixam de importar Agno. A porta tem uma implementação só (exceção justificada: é a fronteira do framework).
Critérios: golden inalterado; `lint-imports` sem as entradas `application -> agno` e `-> infrastructure.tools`; suíte verde.

## F2-05 — `AgnoRuntime` (2/2): ciclo de vida do AgentOS
Escopo: montagem do AgentOS, router AG-UI, router de cancelamento e reaplicação de middlewares saem de `app_factory.py` para `AgnoRuntime.mount(app)`/`close()`; `db=` passado ao AgentOS (avaliar `AsyncMongoDb` do agno: o `MongoDb` síncrono faz I/O no event loop dentro de `arun` — achado da revisão do F1-08); lifespans do AgentOS passam a rodar (B15 — teste vermelho na app real primeiro); `app_factory` fica só com a borda HTTP (CORS, auth, métricas, rotas próprias).
Critérios: rotas e ordem de middlewares iguais às de hoje (testes de borda verdes); lifespan do AgentOS executa (espião); I/O de sessão fora do event loop, ou registrado como resíduo justificado.

## F2-06 — `ConfigStore`: Mongo + YAML
Escopo: porta `ConfigStore` (agents, teams, tools) com duas implementações: Mongo (repositórios atuais) e YAML (arquivo confinado, `yaml.safe_load`, mesmo mapper/validação de domínio); seleção por `CONFIG_STORE=mongo|yaml` (+ `CONFIG_YAML_PATH`). Pendência da F0-03: a suíte de `tests/contract/` roda contra as duas (Mongo via coleção fake; Mongo real como `integration`/`live`) e define a semântica de id duplicado (vence o primeiro na ordem estável, como no F1-10) e da ordem entre lotes de `save_nodes`.
Critérios: mesmo conjunto de agentes/teams/tools carregado por Mongo e YAML equivalentes; documento inválido isolado nas duas; `config.example.yaml` documentado.

## F2-07 — Regra de dependência sem baseline e testes desacoplados
Escopo: `lint-imports` sem baseline nas arestas de `domain`/`application` (contrato `nucleo-sem-frameworks` sem `ignore_imports`; contrato de camadas só com o composition root, se ainda necessário); testes que fazem `patch` de detalhes internos do Agno em `application` reescritos como testes de comportamento com fakes/contratos; módulos movidos saem da lista `ignore_errors` do mypy.
Critérios: baselines de `domain`/`application` vazias; nenhuma redução de cobertura; suíte verde.
