---
name: dev-cycle
description: "Executa um item do roadmap pela cadeia dev → code review (segurança/clean code/arquitetura) → QA, com loop de correção limitado e gate final. Uso: /dev-cycle F1-02 [--commit]"
disable-model-invocation: true
argument-hint: "<item-id> [--commit]"
---

Execute o item `$ARGUMENTS` de `docs/roadmap/` pela cadeia de desenvolvimento. Você (thread principal) **orquestra**: não edita código de produto e não pula estágio. Subagentes não herdam a conversa: cada prompt precisa ser autocontido (id do item, arquivo do item, comandos de gate da fase, achados pendentes).

## 1. Pré-voo
- `git status --porcelain` limpo (exceto retomada declarada). Branch atual = branch de trabalho do item (`Branch:` do item, se houver).
- Leia `CLAUDE.md` e o item. Anote `BASE=$(git rev-parse HEAD)`.
- Crie `.claude/dev-cycle/<item>.json` (ignorado pelo git) com `{"item","base","stage","round","trees":[]}` e atualize a cada estágio: a compactação de contexto não pode perder o contador.

## 2. Dev
Chame `python-dev-specialist` com: id e arquivo do item, gates da fase, regra de não commitar. Espere o `HANDOFF`. `status: blocked` ⇒ pare e leve a pergunta ao usuário.

## 3. Code review
- `git add -A && TREE=$(git write-tree)`; guarde o tree no estado.
- Diff para o revisor: `git diff --cached $BASE` (inclui arquivos novos). Rodadas seguintes: também `git diff <tree anterior> <tree atual>` (delta) e a tabela de achados anterior.
- Chame `security-architecture-reviewer`. Antes e depois confira `git status --porcelain`: a árvore não pode mudar durante a revisão.
- `REQUEST_CHANGES` ⇒ volte ao dev com os achados BLOCKER/MAJOR (MINOR a critério do dev, justificando). Cada achado é corrigido ou refutado com evidência. Depois, nova revisão sobre o delta.
- `BLOCK` ⇒ pare e escale ao usuário.

## 4. QA
- Snapshot `git status --porcelain` antes. Chame `qa-engineer` com o item, o diff e os gates.
- Depois: qualquer mudança fora de `tests/` e `docs/qa/` invalida o QA (reverta só esses arquivos e conte como violação).
- Os testes novos do QA passam por uma revisão curta do revisor (só o delta de `tests/`).
- `FAIL` por bug de produção ⇒ volta ao dev com a repro; depois revisão do delta e QA de novo.

## 5. Limite
No máximo **3 rodadas** somando voltas de revisão e de QA. Estourou ⇒ pare e escale ao usuário com um resumo (achados em aberto, o que foi tentado).

## 6. Gate final (você mesmo roda; não confie em relato)
Os comandos de gate da fase atual, listados em `CLAUDE.md`. Tudo verde ou o item não fecha.

## 7. Fechamento
- Com `--commit`: commit local com mensagem convencional (`feat|fix|chore(<escopo>): ... [<item>]`), corpo com resumo e a linha de co-autoria exigida pelo ambiente. **Nunca** push nem PR.
- Sem `--commit`: deixe a árvore alterada e mostre a mensagem de commit pronta.
- Atualize o status do item no `docs/roadmap/README.md` e resuma ao usuário: o que mudou, rodadas, achados relevantes, gates.
