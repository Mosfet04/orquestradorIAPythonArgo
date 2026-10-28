---
name: dev-cycle
description: "Executa um item do roadmap pela cadeia dev → code review (segurança/clean code/arquitetura) → QA, com loop de correção limitado e gate final. Uso: /dev-cycle F1-02 [--commit]"
disable-model-invocation: true
argument-hint: "<item-id> [--commit]"
---

Execute o item `$ARGUMENTS` de `docs/roadmap/` pela cadeia de desenvolvimento. Você (thread principal) **orquestra**: não edita código de produto e não pula estágio. Subagentes não herdam a conversa: cada prompt precisa ser autocontido (id do item, arquivo do item, comandos de gate da fase, achados pendentes).

## Economia de contexto (vale para todo o ciclo)

Cada turno seu relê a conversa inteira; contexto grande aqui custa mais que em qualquer subagente.

- **Um item por contexto.** Se esta conversa já executou outro item (ou passou de ~100k tokens), peça ao usuário um `/compact` (ou `/clear`) antes de começar; o estado em `.claude/dev-cycle/<item>.json` permite retomar.
- **Você não lê código nem diff.** Nada de `Read` em `src/`/`tests/`, `git diff` com conteúdo ou `pytest` exploratório: revisor e QA buscam o diff sozinhos a partir de `BASE`/`TREE`. Você lê o item, os relatórios (HANDOFF/REVIEW/QA), `git status --porcelain`, `git diff --stat` e a saída do gate final.
- Espere a notificação de subagente em background; não faça polling com `ListAgents`.

## 1. Pré-voo

- `git status --porcelain` limpo (exceto retomada declarada). Branch atual = branch de trabalho do item (`Branch:` do item, se houver).
- Leia `CLAUDE.md` e o item. Anote `BASE=$(git rev-parse HEAD)`.
- Crie `.claude/dev-cycle/<item>.json` (ignorado pelo git) com `{"item","base","stage","round","trees":[]}` e atualize a cada estágio: a compactação de contexto não pode perder o contador.

## 2. Dev

- **Modelo do dev:** use o `Modelo:` do item, se houver. Sem ele: `model: "sonnet"` quando o item é de ferramental/periferia (CI, docs, dev-env, compose, lint/Codacy, dependências sem mudança de comportamento) e não toca authn/authz, segredos, SSRF, tools, RAG, runtime nem contratos de porta; caso contrário, o padrão do agente (Opus). Na dúvida, Opus. Registre a escolha no estado.
- Chame `python-dev-specialist` com: id e arquivo do item, gates da fase, regra de não commitar. Espere o `HANDOFF`. `status: blocked` ⇒ pare e leve a pergunta ao usuário.

## 3. Code review

- `git add -A && TREE=$(git write-tree)`; guarde o tree no estado.
- Prompt do revisor: `BASE`, `TREE` e o comando do diff (`git diff --cached $BASE`, inclui arquivos novos); ele roda o diff, você não. Rodadas seguintes: também o delta (`git diff <tree anterior> <tree atual>`) e a tabela de achados anterior.
- Chame `security-architecture-reviewer`. Antes e depois confira `git status --porcelain`: a árvore não pode mudar durante a revisão.
- `REQUEST_CHANGES` ⇒ volte ao dev com os achados BLOCKER/MAJOR (MINOR a critério do dev, justificando). Cada achado é corrigido ou refutado com evidência. Depois, nova revisão sobre o delta.
- **Rodada de correção:** continue o dev anterior via `SendMessage` só se o resultado dele reportou `total_tokens` ≤ 150k; acima disso, chame um `python-dev-specialist` novo (mesmo modelo) com o item, `BASE`, os achados e os arquivos tocados — contexto limpo sai mais barato que reler 200k+ a cada turno.
- `BLOCK` ⇒ pare e escale ao usuário.

## 4. QA

- Snapshot `git status --porcelain` antes. Chame `qa-engineer` com o item, o comando do diff e os gates.
- Depois: qualquer mudança fora de `tests/` e `docs/qa/` invalida o QA (reverta só esses arquivos e conte como violação).
- Os testes novos do QA passam por uma revisão curta do revisor (só o delta de `tests/`).
- `FAIL` por bug de produção ⇒ volta ao dev com a repro (mesma regra de continuar vs. dev novo); depois revisão do delta e QA de novo.

## 5. Limite

No máximo **3 rodadas** somando voltas de revisão e de QA. Estourou ⇒ pare e escale ao usuário com um resumo (achados em aberto, o que foi tentado).

## 6. Gate final (você mesmo roda; não confie em relato)

Os comandos de gate da fase atual, listados em `CLAUDE.md`. É a única execução autoritativa da suíte completa no ciclo. Tudo verde ou o item não fecha. Traga para o contexto só o resumo (`pytest -q` / `| tail -n 15`); em falha, a parte relevante.

## 7. Fechamento

- Com `--commit`: commit local com mensagem convencional (`feat|fix|chore(<escopo>): ... [<item>]`), corpo com resumo e a linha de co-autoria exigida pelo ambiente. **Nunca** push nem PR.
- Sem `--commit`: deixe a árvore alterada e mostre a mensagem de commit pronta.
- Atualize o status do item no `docs/roadmap/README.md` e resuma ao usuário: o que mudou, rodadas, modelo do dev, achados relevantes, gates. Lembre que o próximo item deve começar após `/compact` (ou `/clear`).
