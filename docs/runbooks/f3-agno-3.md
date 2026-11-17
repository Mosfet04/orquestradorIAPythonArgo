# Runbook: virada do agno 2.5.8 para o 3.1.12 (F3)

Para quem opera o deploy. O release com o agno 3.1.12 muda o esquema das sessões: no 2.5.8 cada run ficava no campo `runs` do documento da sessão (`agno_sessions`); no 3.x cada run é um documento de `agno_runs`, com índice único em `run_id`. A migração copia os runs, **não apaga o campo legado** e o `get_session` do 3.x lê os dois: sessão ainda não migrada continua legível.

Escopo de dados: o que muda de formato são as sessões (runs para `agno_runs`). Além disso, o `MigrationManager.up()` carimba a versão do esquema de cada tabela do agno em `agno_schema_versions`, cria índices de `schedules` e refaz a chave de `agno_learnings` (o re-key só se desfaz por restore). Configs (`agents_config`, `teams_config`, `tools`), memórias e RAG não mudam de formato. O índice vetorial de um `rag_<agent_id>` criado no 2.5.8 é refeito uma vez no startup do agente (remoção assíncrona; ver F3-03).

## Regras que não se negociam

- Backup completo antes de tudo. O restore do backup (com as coleções novas apagadas, ver [Rollback](#rollback)) é a única garantia de volta ao estado anterior.
- Migrar logo depois do deploy e **antes de liberar tráfego**: a v3.0.0 troca, por `run_id`, o documento de `agno_runs` pela cópia do campo legado. Um run legado que o 3.x já regravou (`regenerate`/`continue` do dono, por exemplo) volta ao estado do 2.5.8.
- O cleanup do campo legado (`cleanup_legacy_runs_field`) **nunca roda no mesmo release** da migração. Ele é manual, num release posterior, depois de o 3.x ter rodado estável e de o backup pós-migração existir. O módulo de migração não o chama (há teste sobre a fonte).
- Nada de `POST /databases/{id}/migrate` com `target_version` menor que a atual em produção com tráfego (ver [Rollback](#rollback)).

## Comando de migração

Módulo da imagem nova (`agno` 3.1.12), que lê `MONGO_CONNECTION_STRING` e `MONGO_DATABASE_NAME` do ambiente do app (o mesmo da aplicação; nada de URI na linha de comando):

```bash
python -m src.infrastructure.runtime.agno.session_migration            # migra (up) e confere
python -m src.infrastructure.runtime.agno.session_migration --check    # só confere; não grava nem cria coleção
python -m src.infrastructure.runtime.agno.session_migration --down     # desfaz a v3.0.0 (esquema 2.5.6)
```

`--check` e `--down` são exclusivos. Sem flag: `MigrationManager(db).up()` e depois a conferência. Código de saída:

| Saída | Significado |
|---|---|
| 0 | conferência ok (ou `--down` concluído; depois do `down` a conferência acusa "migração pendente", mas sai com 0) |
| 1 | a conferência achou pendência (linha `conferência: há pendência`) |
| 2 | falha do Mongo, da configuração ou da migração; a mensagem traz só o tipo do erro |

A conferência é ok quando: esquema de sessões carimbado em 3.0.0 ou acima, nenhum run legado fora de `agno_runs`, nenhum conflito de dono, índice único de `run_id` em `agno_runs` presente e nenhum `run_id` repetido. A migração é idempotente: rodar duas vezes dá o mesmo resultado.

Com Docker Compose, rode o módulo num container descartável da imagem nova, sem subir o app e sem publicar porta (acrescente `-f docker-compose.dev.yml` no ambiente de desenvolvimento, como no README):

```bash
docker compose run --rm --no-deps app python -m src.infrastructure.runtime.agno.session_migration --check
```

O mesmo vale para `up` (sem flag) e `--down`. O `--no-deps` não sobe o Mongo: com um Mongo externo ele precisa estar acessível a partir do container; no desenvolvimento (Atlas Local do `docker-compose.dev.yml`), suba-o e espere ficar saudável antes (`$COMPOSE up -d mongodb`, com `COMPOSE` como no README).

## Pré-checagens

1. Janela sem escrita: nenhum processo gravando sessões. O app antigo é parado no passo 1 da virada (o Compose tem `restart: unless-stopped`, então ele só fica parado depois de um `stop`).
2. Mongo acessível pela mesma configuração do app, com usuário que escreve em `agno_sessions`, `agno_runs` e `agno_schema_versions`.
3. Documentos de agente e team: os ids reservados do agno 3.x (`os-manager-internal`, `platform-builder-internal`, `platform-manager-internal`, `company-manager-internal`, `product-manager-internal`) passam a ser documento inválido. Confira `agents_config` e `teams_config` antes do deploy.
4. Imagem/commit anterior identificado e guardado para o rollback (o último commit no agno 2.5.8 é o `774683f`; use a tag de produção que você de fato roda).
5. Espaço para o dump e para os documentos novos (os runs ficam duplicados, no campo legado e em `agno_runs`, até o cleanup).

## Ordem da virada

Backup → deploy → migrate → verificar → liberar tráfego.

### 1. Parar o app antigo e fazer o backup completo

Primeiro pare o app que está servindo (`docker compose stop app`, ou tire a réplica do ar), para o dump não pegar escrita pela metade. Depois, dump de todo o banco (`MONGO_DATABASE_NAME`, padrão `agno`), com todas as coleções (`agno_sessions`, `agno_memories`, `agents_config`, `teams_config`, `tools`, `rag_*`...), num arquivo único.

Mongo externo ou de produção: carregue o `.env` no seu shell (`set -a; . ./.env; set +a`; assim as variáveis só existem no ambiente, não no `argv`) e passe a URI por um arquivo de config do `mongodump` (`--config`, recurso das Database Tools 100.x ou mais novas), com permissão restrita e apagado depois:

```bash
umask 077
printf 'uri: "%s"\n' "$MONGO_CONNECTION_STRING" > "$HOME/mongodump.yml"
mongodump --config "$HOME/mongodump.yml" --db "$MONGO_DATABASE_NAME" --gzip \
  --archive="agno-pre-f3-$(date +%Y%m%d-%H%M).archive.gz"
rm -f "$HOME/mongodump.yml"
```

Atlas Local do `docker-compose.dev.yml`: a URI do `.env` aponta para o host `mongodb`, que só resolve dentro da rede do Compose. Rode o dump dentro do container, como o README faz no restore (o container lê usuário e senha das `MONGODB_INITDB_ROOT_*`; a senha aparece no `argv` dentro dele). O README mostra `mongorestore` e `mongosh` nesse container; o `mongodump` não aparece lá (só no `mongo:7` antigo), então confirme que ele existe na imagem (`$COMPOSE exec mongodb mongodump --version`) e, se não existir, use as Database Tools do seu host contra a porta publicada do Mongo:

```bash
umask 077   # o arquivo do backup sai só com permissão do dono
COMPOSE="docker compose -f docker-compose.yml -f docker-compose.dev.yml"
$COMPOSE exec -T mongodb sh -c 'mongodump --archive --gzip --db agno \
  --username "$MONGODB_INITDB_ROOT_USERNAME" --password "$MONGODB_INITDB_ROOT_PASSWORD" \
  --authenticationDatabase admin' > "agno-pre-f3-$(date +%Y%m%d-%H%M).archive.gz"   # troque agno pelo MONGO_DATABASE_NAME
```

Guarde o arquivo fora do servidor do Mongo e **confira que ele restaura**: o teste que vale é restaurar num Mongo descartável e abrir os dados (`mongorestore --dryRun` só valida a leitura do arquivo, e as flags `--config`/`--dryRun` não foram exercitadas por este runbook: confira-as na versão das Database Tools que você usa). O dump não leva os índices de busca vetorial; o agno os recria em `rag_<agent_id>` no próximo startup do agente.

### 2. Deploy sem tráfego

Suba a imagem nova (agno 3.1.12) mantendo o app fora do balanceador. No Compose, basta não subir o `app` ainda: o passo 3 usa um container descartável da mesma imagem (`docker compose build`/`pull` antes). O app 3.x não migra sozinho no startup.

### 3. Conferir e migrar

```bash
# estado de partida (só leitura): esperado "migração pendente" e, no máximo, os conflitos que você já conhece
python -m src.infrastructure.runtime.agno.session_migration --check
```

**Conflitos de `run_id`** (linha `runs legados em agno_runs com outra sessão ou outro user_id`):

- Antes do `up`, é um run gravado por outro dono com o `run_id` de um run legado (alguém usou o 3.x antes da migração). O `up` o troca pelo run do dono. Se esse run alheio precisa ser mantido, trate à mão **antes** do `up` (inspecione os documentos em `agno_runs` por `run_id`); o backup guarda o original.
- Depois do `up`, é o mesmo `run_id` em duas sessões legadas (o 2.5.8 aceitava): fica com a última sessão copiada, e a conferência segue em 1 até você tratar à mão (decidir qual sessão mantém o run).

Sem conflito, migre:

```bash
python -m src.infrastructure.runtime.agno.session_migration
```

A migração copia `sessions.runs` para `agno_runs` (upsert por `run_id`, com a `session_id` e o `user_id` da sessão dona), cria os índices de `schedules`, refaz a chave de `agno_learnings` e carimba o esquema em 3.0.0. Não apaga nada.

### 4. Verificar

1. Saída 0 e a última linha `conferência: ok`. Esperado na saída: `runs legados fora de agno_runs: 0`, `índice único de run_id em agno_runs: sim`, `run_id repetido em agno_runs: 0`.
2. Rode `--check` de novo: mesma saída, saída 0.
3. Saída 1: não libere tráfego; leia a linha que acusou a pendência. Saída 2: o Mongo ou a config falhou; a migração pode ter parado no meio, e rodar de novo é seguro (idempotente), mas confira com `--check`. O log do próprio agno, na falha, traz o **texto da exceção** (que pode incluir host do Mongo): confira o destino do log antes de abrir ticket ou colar a saída em canal público.
4. Suba o app (já sem tráfego externo) e confira, com a chave admin ou run, `GET /health` e o histórico de uma sessão conhecida (`GET /sessions/{session_id}/runs`): os runs antigos aparecem.

### 5. Liberar tráfego

Só com a verificação ok. A partir daqui, o rollback por restore ou `--down` tem custo (ver [Rollback](#rollback)).

## Rollback

O que o 3.x deixa no banco e o 2.5.8 ignora: a coleção `agno_runs`, o carimbo `3.0.0` em `agno_schema_versions` e, nas sessões que o 3.x tocou, a ausência do campo `runs` inline. O `MigrationManager.up()` **pula** uma tabela carimbada em 3.0.0 ou acima (`agno/db/migrations/manager.py`, "Skipping migration"). Por isso, depois de qualquer volta ao 2.5.8, uma nova virada só migra se o carimbo tiver saído; sem isso o `up` não copia nada e o `--check` fica em 1 sem saída pelo `up`.

**Antes de liberar tráfego**, basta voltar a imagem anterior: o `up` não apaga o campo `runs` das sessões, então o 2.5.8 enxerga tudo como estava.

**Depois de liberar tráfego**, as sessões tocadas pelo 3.x perdem o campo `runs` inline, e o 2.5.8 deixa de ver esse histórico. Duas saídas:

- **Restore** (a garantia): perde tudo que foi escrito depois do backup (sessões, memórias, alterações de config pela API). O `mongorestore --drop` só derruba as coleções que existem no dump, e o backup do 2.5.8 não tem `agno_runs` nem `agno_schema_versions`. **Antes do restore**, apague essas duas e qualquer outra coleção criada depois do backup, ou esvazie o banco (`db.dropDatabase()`) e restaure no **mesmo nome**; o `--nsInclude "<banco>.*"` não funciona para restaurar em outro nome sem `--nsFrom`/`--nsTo`. Sem isso sobram os runs do 3.x e o carimbo 3.0.0. Depois do restore, volte a imagem anterior.

  ```bash
  # Mongo externo: abra o mongosh/Compass com a senha pedida no prompt (sem URI com senha no argv) e rode
  #   db.agno_runs.drop(); db.agno_schema_versions.drop()
  # Atlas Local (troque agno pelo MONGO_DATABASE_NAME nos dois comandos):
  $COMPOSE exec -T mongodb sh -c 'mongosh --username "$MONGODB_INITDB_ROOT_USERNAME" \
    --password "$MONGODB_INITDB_ROOT_PASSWORD" --authenticationDatabase admin agno \
    --eval "db.agno_runs.drop(); db.agno_schema_versions.drop()"'

  # restore (mongodump.yml criado como no passo 1; apague depois)
  mongorestore --config "$HOME/mongodump.yml" --archive=<arquivo do passo 1> --gzip --drop \
    --nsInclude "$MONGO_DATABASE_NAME.*"
  # restore no Atlas Local (troque agno pelo MONGO_DATABASE_NAME)
  $COMPOSE exec -T mongodb sh -c 'mongorestore --archive --gzip --drop --nsInclude "agno.*" \
    --username "$MONGODB_INITDB_ROOT_USERNAME" --password "$MONGODB_INITDB_ROOT_PASSWORD" \
    --authenticationDatabase admin' < <arquivo do passo 1>
  ```
- **`--down`** (app parado; só se o 2.5.8 ainda não serviu tráfego depois da volta): `python -m src.infrastructure.runtime.agno.session_migration --down` devolve os runs de `agno_runs` ao campo `runs` das sessões, apaga `agno_runs` e baixa o carimbo; só então volte a imagem anterior. O `down` **não é rollback completo**: sobrescreve o campo `runs` das sessões com o conteúdo de `agno_runs` (inclui os runs que o 3.x gravou) e, por isso, descartaria os runs que um 2.5.8 em serviço tivesse gravado no campo; apaga a coleção `agno_runs`; e **não reverte o re-key de `agno_learnings`**. Em caso de dúvida, restore.

### Repetir a virada depois de voltar ao 2.5.8

Há duas formas de deixar o `up` rodar de novo:

- **O 2.5.8 não serviu tráfego** (rollback antes de liberar): `--down` ou apagar `agno_schema_versions`.
- **O 2.5.8 serviu tráfego**: apague a coleção `agno_schema_versions` (por exemplo `db.agno_schema_versions.drop()` no `mongosh`) e **não use o `--down`**, que sobrescreveria com `agno_runs` os runs que o 2.5.8 gravou no campo `runs`. O `up` volta a rodar em todas as tabelas e copia o campo legado por upsert; depois confira com `--check` (saída 0).

Repetir o `up` sem o carimbo é seguro (conferido no código do agno 3.1.12, `v3_0_0.py`):

- Runs: `replace_one({"run_id"}, ..., upsert=True)` por run do campo legado. Cada run presente no campo legado volta ao estado dele em `agno_runs` (um run legado que o 3.x regravou, como `regenerate`/`continue`, volta ao estado do 2.5.8).
- Índices de `schedules`: o `up` só garante os índices (remove o único antigo de `name`, cria os do 3.x); repetido, não muda nada. Falha só com nomes de agendamento duplicados por dono.
- Re-key de `agno_learnings`: as linhas já com a chave nova são contadas como "keyed" e puladas.
- Sobras de `agno_runs` da primeira tentativa: documentos cujo `run_id` não está no campo legado (runs criados pelo 3.x) **ficam** e continuam lidos pelo `get_session` do 3.x: são o histórico do período no 3.x, não lixo. Os de sessão que o 2.5.8 apagou nesse meio tempo ficam sem sessão; não aparecem em nenhuma conversa e podem ser removidos à mão, e o `--check` não os acusa.

A rota do agno `POST /databases/{id}/migrate?target_version=<menor que a atual>` (só `API_KEY_ADMIN`; `{id}` é o id do db do AgentOS) faz esse mesmo `down` **dentro do processo vivo**. Só use com o app sem tráfego: o run gravado entre o agrupamento e o `drop()` de `agno_runs` se perde. Sem `target_version`, a rota migra até a última versão (resposta 200); `POST /databases/all/migrate` faz o mesmo para todos os dbs. Prefira o módulo, que imprime a conferência.

## Cleanup do campo legado (release posterior)

Não faz parte desta virada. O `cleanup_legacy_runs_field` do agno recusa rodar (`RuntimeError`) enquanto houver sessão com `runs` não nulo, que é o estado normal depois da migração; só passa com `force=True`. Use só num release seguinte, manualmente, com o 3.x estável, `--check` em ok e um backup novo feito depois da migração. Depois dele o campo `runs` deixa de existir nas sessões: o 2.5.8 só volta a ver histórico pelo `--down` (que reconstrói o campo a partir de `agno_runs`, então depende de `agno_runs` íntegra) ou por restore.

## Mudanças de contrato (REST, AG-UI e rotas)

O que muda para clientes e integradores entre o 2.5.8 e o 3.1.12 (decisões do dono, aceitas; detalhes nos README):

**Sessões e dono**

- Sessão de outro `user_id`: REST e AG-UI respondem `404 {"detail": "Session not found"}` antes do run (o 2.5.8 rodava numa conversa à parte); anônimo em sessão que tem dono também. A chave `API_KEY_ADMIN` não é bypass: a chave da API não é uma identidade.
- `run_id` já gravado em outra sessão ou de outro `user_id`: no REST (`run_id` do form) a resposta é 200 e o run roda, mas **não é gravado** (o agno loga um aviso) e o run do dono fica como estava. No AG-UI, um `runId` já gravado (de qualquer sessão ou usuário) é trocado por um UUID4 do servidor, devolvido no `RUN_STARTED`: o cliente deve usar o `runId` dos eventos. Regravar o próprio run pelo REST (`continue`, `regenerate`) segue valendo.
- Mongo de sessões fora: o run falha (REST e AG-UI respondem 500 sem o texto do erro); o 2.5.8 seguia sem sessão.

**AG-UI**

- O histórico da conversa vem da sessão gravada no servidor para o `threadId`, não das mensagens anteriores enviadas pelo cliente: só a última mensagem `user` e `forwardedProps.user_id` entram; `state`, client tools, `context` e mídia são ignorados (contrato novo desde o F3-04).
- `TOOL_CALL_RESULT` vem serializado em JSON com escape ASCII.
- O conversor do 3.x também emite `REASONING_*`, `STATE_*`, `CUSTOM` e `RAW`; o filtro de saída os descarta (a lista permitida está no README).
- Cancelamento: cancelar o team cancela o membro em andamento; cancelar o membro encerra o team como cancelado (AG-UI `RUN_ERROR` com `code` `run_cancelled`).

**Rotas REST do AgentOS**

- `background=true` sem `stream=false` vira SSE (stream é o default); para resposta em background sem stream, mande `stream=false`.
- `GET /models` deixou de existir.
- `POST`/`DELETE /knowledge/content` respondem 503 sem knowledge no AgentOS (antes 400/422).
- `/schedules` responde 200 (o Mongo implementa a agenda); `/approvals*` segue 503.
- Novas rotas administrativas (só `API_KEY_ADMIN`): `/queue*`, `/service-accounts*`, `/filesystem*`, `/learnings*`. O total de rotas do AgentOS passa de 101 para 140 no app montado.
- `GET /agents/{id}`: a chave `memory` continua no 3.1.12, mas seu conteúdo é o do 3.x (a configuração de memória usa `update_memory_on_run`); não dependa do formato de `enable_user_memories`, removido.

**Configuração dos agentes**

- `reasoning: true` vira `ReasoningTools(add_instructions=True)`: o agente ganha as tools `think`/`analyze` e as usa no próprio run, **sem fase separada de raciocínio e sem `reasoning_max_steps`** (o campo saiu). Faz mais chamadas ao modelo por resposta. As tools aparecem como `TOOL_CALL_*` no AG-UI (o texto do raciocínio vai nos argumentos) e nas tools do run REST.
- Id reservado do agno em agente ou team (lista nas pré-checagens): documento inválido (`InvalidField("id")`).

**Outros**

- Os runs ficam em `agno_runs` (um documento por run, chave `run_id`); o `get_session` junta os da coleção com os do campo legado.
- Telemetria (só com `AGNO_TELEMETRY=true`) sai numa thread em fila, não no fim do run. O app assume `AGNO_TELEMETRY=false`. Resíduo conhecido: `/eval-runs` ignora a variável (tratado no F3-08).
- RAG: provider de embedding fora na indexação vira `EmbeddingError` tratado pelo agno (conteúdo `FAILED`): nenhum chunk gravado, o agente sobe com o RAG vazio e o próximo startup indexa. O log do agno leva o texto do erro do provider.
