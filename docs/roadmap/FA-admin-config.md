# FA — API Admin de configuração e troca a quente

Objetivo: criar, alterar e remover agents, teams e tools por HTTP, com validação pelas entidades de domínio, e servir a mudança **sem reiniciar o processo**. Destrava o playground (`../playgroundOrquestrador`, fases P2–P4), que deixa de gravar direto no Mongo. Decisões do usuário em `../playgroundOrquestrador/docs/decisions/0001-gestao-realtime.md`.

Ordem: entre F2 e F3. Só o FA-04 toca o Agno, e fica confinado no `AgnoRuntime` (`src/infrastructure/runtime/agno/`); o teste de contrato dele entra na matriz 2.5.8 × 3.1.x da F3. Nada de `AgentOS.resync` (API interna que muda na 3.x): a troca usa o que o roteamento já lê a cada request (`get_agent_by_id(agents=os.agents, ...)`, `agno/os/routers/agents/router.py:457` no 2.5.8).

Princípios (valem para todos os itens):

- A validação é a das entidades (`AgentConfig`, `TeamConfig`, `Tool`, `RagConfig`, `ModelConfig`): nada de schema paralelo. Erro vira 422 por campo, sem `str(exc)` e sem eco do valor.
- Escrita só com a chave admin (o prefixo `/admin` já é admin-only em `api_key_auth.py`).
- Documentos Mongo existentes continuam válidos; campos novos (`version`, `updated_at`, `updated_by`) são opcionais. O mapper legado (`config_documents.py`) continua lendo camelCase; ao salvar, o documento é regravado em snake_case.
- Store YAML continua só leitura: escrita responde 409 (`ConfigStoreReadOnly`). Para YAML, a troca a quente vem do `POST /admin/config/reload`.
- Anti over-engineering: um caso de uso de config em `application`, sem registry nem barramento de eventos.

## FA-01 — Escrita nos repositórios de config
Escopo: `save(entity, expected_version)` e `delete(id, expected_version)` nas portas `IAgentConfigRepository`, `ITeamConfigRepository` e `IToolRepository` (`src/domain/repositories/`); listagem de todos os documentos (inclusive inativos, para a UI) separada do `get_active_*` usado pelo runtime. Mongo implementa com `version` inteiro (update condicional `{"id", "version"}`; divergência ⇒ `ConfigVersionConflict`); YAML levanta `ConfigStoreReadOnly`. Índice único de `id` já existe no seed.
Critérios: dois `save` concorrentes com a mesma versão ⇒ um vence, o outro recebe conflito; documento legado sem `version` é tratado como versão 0; YAML recusa escrita sem tocar o arquivo.
Testes: contrato da porta rodando nas duas implementações (Mongo com o fake do `tests/fakes/`, YAML).
Resultado (decisão registrada): as três portas ganharam `list_all() -> list[Versioned[E]]` (todos os documentos, inclusive inativos e ids repetidos, na ordem estável), `save(entity, expected_version) -> int` (nova versão) e `delete(entity_id, expected_version)`, com os mesmos nomes nas três (o CRUD do FA-02 fica genérico). `Versioned`, `ConfigVersionConflict` (`entity_id`, `expected_version`) e `ConfigStoreReadOnly` em `src/domain/repositories/versioning.py`; `version` fica fora das entidades (é metadado do documento). Mongo: base `MongoConfigRepository[ConfigEntityT]` (`mongo_base.py`; o `TypeVar` fica em `config_documents.py`) com `list_all`/`save`/`delete` e o laço de leitura que isola documento inválido (antes copiado nos três repositórios); filtro `{"id", "version"}` (versão 0 = `{"$in": [0, null]}`, o legado), que só aceita id texto e versão inteira (`TypeError` antes do Mongo: id operador como `{"$ne": ""}` casaria o documento de outro id); `Tool` passou a exigir id texto (`require_text`, como agente e team; documento de tool com id não texto agora é isolado com log). `save` = `replace_one` condicional, sem `upsert`, do documento inteiro no mesmo `_id`, em snake_case (`entity_document`, `config_documents.py`), com `version` e `updated_at` (UTC): campo fora da entidade (inclusive o legado camelCase) é descartado. Sem casar: com `expected_version` 0 e nenhum documento com o id, `insert_one`; senão (ou `DuplicateKeyError`) conflito. `version` que não é inteiro ≥ 0 invalida o documento só na `list_all` (`document_version`, com log): o runtime segue servindo o documento e toda escrita nele conflita até a `version` ser corrigida no banco. `updated_by` fica para o FA-02/FA-05, que conhecem o ator. YAML: `list_all` lê a `version` do documento (sem ela, 0); `save`/`delete` levantam `ConfigStoreReadOnly` sem ler nem gravar o arquivo. Fakes: repositórios em memória com a mesma semântica (no contrato); `FakeMongoCollection.insert_one` (com fidelidade no `test_mongo_collection_fidelity_contract.py`). Testes: `tests/contract/test_config_write_contract.py` (memória, Mongo fake e `mongo-live` para escrita, sem o índice único de `id`; os quatro backends para `list_all`; YAML só leitura com bytes e `mtime` do arquivo inalterados) e `tests/unit/test_mongo_config_writes.py` (legado camelCase regravado em snake_case no mesmo `_id`; `version` inválida; criação que perde a corrida no índice único). Resíduo: só a criação realmente simultânea de um id novo depende do índice único de `id` do seed (o app não o cria); sem ele, as duas criações gravariam. Substituição, `save` de legado e criação sequencial não dependem do índice.

## FA-02 — CRUD `/admin/config/{agents,teams,tools}`
Escopo: `GET` (lista paginada, inclui inativos) e `GET /{id}`; `POST`; `PUT /{id}` com `If-Match` (ETag = `version`; ausente ⇒ 428, divergente ⇒ 412); `DELETE /{id}` = soft (`active: false`), `?hard=true` = remoção. Integridade referencial: `member_ids` do team e `tools_ids` do agent precisam existir (422); remover tool/agent em uso ⇒ 409 com a lista de quem usa. `GET` de tool devolve headers mascarados. Rotas em `presentation`, caso de uso em `application`.
Critérios: corpo com operador Mongo (`{"$ne": ...}`) ⇒ 422; erro de validação não ecoa o valor (inclusive `api_key_ref` malformada); nenhuma rota nova acessível com a chave run (403).
Testes: integração das rotas com `httpx.ASGITransport` e fakes; segurança (injeção, eco, authz).

## FA-03 — `/admin/schema`
Escopo: descreve o que a UI pode oferecer, a partir do código em vigor: providers por tipo (`ProviderRegistry.supported(kind)`, `providers/registry.py:189`), `model_params` aceitos por provider (`providers/builtins.py`), se exige `base_url`, modos de team, tipos de parâmetro de tool, métodos HTTP, `search_strategy` de RAG, formatos de referência de segredo, e se o config store é gravável.
Critérios: plugin de provider permitido pela allowlist aparece no schema sem mudar a rota; nenhum valor de env/segredo no corpo.
Testes: unit do montador do schema; integração da rota.

## FA-04 — Troca a quente do runtime
Escopo: caso de uso de config faz **build → persist → swap**: monta todos os agents/teams ativos com a mudança aplicada (fora do caminho de request); só com o build ok persiste e troca; falha ⇒ nada gravado, runtime anterior segue, resposta com o motivo (sem `str(exc)`). Swap atômico sob `asyncio.Lock` no `AgnoRuntime` (método novo na porta `AgentRuntime`): substitui `os.agents`/`os.teams` e um holder mutável lido pelos routers próprios (`build_agui_router`, `build_run_cancel_router`), que hoje capturam as listas na montagem; incrementa `runtime_revision` (exposto no `/admin/health` e na resposta da escrita). Runs em andamento terminam com a instância antiga. `POST /admin/config/reload` relê o store e troca (caminho do YAML e recuperação manual); substitui o `/admin/refresh-cache`, que só recarrega um cache que ninguém serve (manter como alias deprecado).
Critérios: editar as instruções de um agente ⇒ o run seguinte usa as novas, sem restart; run em stream iniciado antes termina com as antigas; config que não monta ⇒ 422 e o run seguinte usa a versão anterior; `POST /agui/{id}` de entidade criada depois do startup funciona; entidade removida ⇒ 404 nas rotas do AgentOS e no AG-UI.
Testes: contrato do swap; integração com stream em andamento durante a troca; concorrência (duas escritas simultâneas serializadas).
Riscos: o router AG-UI usa a instância compartilhada (resíduo F6-01); instâncias antigas só são liberadas quando os runs terminam.

## FA-05 — Auditoria e propagação entre réplicas
Escopo: coleção `config_audit` (ator = id da chave, data, tipo, id, versão, diff com headers e segredos mascarados) gravada na mesma operação da escrita; `GET /admin/config/{kind}/{id}/history` e restauração de uma versão (que passa pelo mesmo build → persist → swap). Documento `config_revision` incrementado a cada escrita; cada processo consulta a cada ~2 s (`CONFIG_REVISION_POLL_SECONDS`) e, se mudou, relê e troca. Sem change streams.
Critérios: segunda réplica (dois apps no mesmo Mongo nos testes) passa a servir a mudança em até um ciclo de poll; histórico nunca contém valor de segredo.
Testes: integração com dois apps e o fake de Mongo; segurança (mascaramento).

## FA-06 — SSRF e headers por referência na escrita
Escopo: antecipa da F4-01/F5-05 só o necessário para a escrita pela API: a `route` da tool e a `base_url` de modelo/embedder passam pela guarda de `src/infrastructure/security/ssrf.py` ao salvar (esquema, IP privado/link-local/metadata, loopback só no modo dev local); headers de tool aceitam valor por referência (`env:`/`file:`, mesma regra de `parse_secret_ref`) resolvido só na chamada, e o GET devolve os valores mascarados. Header em texto continua lido (compatibilidade), mas a API não aceita gravar novo valor em texto para nomes sensíveis (`Authorization`, `*-Key`, `*-Token`, `Cookie`).
Critérios: tool com `route` para `169.254.169.254` ⇒ 422; header por referência resolve na chamada e nunca aparece em log, trace ou resposta.
Testes: segurança (SSRF por IP literal, nome que resolve para IP privado, redirect); unit da resolução de header.
Riscos: DNS rebinding e redirect dos SDKs seguem na F4-01.

## FA-07 — Campos hoje fixos (fase B)
Não bloqueia a F3: pode rodar depois dela, quando o playground chegar à P5.
Escopo: campos opcionais em `AgentConfig`/`TeamConfig` para o que hoje é fixo em `agent_factory_service.py:433-467` (`markdown`, `num_history_runs`, `add_datetime_to_context`, `reasoning`...), com default igual ao comportamento atual e entrada no `/admin/schema`.
Critérios: documento sem os campos gera exatamente os mesmos kwargs (golden inalterado).
Testes: golden; unit do mapper.
