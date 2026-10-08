# Roadmap — orquestrador plug-and-play e agnóstico a fornecedores

Ordem de execução = numeração. Cada item passa por `/dev-cycle <item> --commit` (dev → code review → QA → gate final) e cabe em ~600 linhas de diff. Branch de trabalho: `feat/plug-and-play` (a partir de `origin/main`). O WIP de telemetria original está preservado em `wip/telemetria`.

| Fase | Entrega | Depende de | Arquivo |
|---|---|---|---|
| F0 Fundação | `pyproject.toml`, lock com hashes, ferramentas, CI mínimo, fakes e golden tests | — | [F0-fundacao.md](F0-fundacao.md) |
| F1 Estabilização + higiene | Corrige o que o README promete e não funciona; fecha a borda (auth, CORS, telemetria, container) sem mudar a arquitetura | F0 | [F1-estabilizacao.md](F1-estabilizacao.md) |
| F2 Núcleo plugável | Registry de providers (+ OpenAI-compatível), entidades neutras, Agno confinado no `AgnoRuntime`, config store Mongo + YAML | F1 | [F2-nucleo-plugavel.md](F2-nucleo-plugavel.md) |
| F3 Migração Agno 3.x | Bump só no adapter, migração do esquema, runbook | F2 | [F3-F9.md](F3-F9.md) |
| F4 Tools + MCP cliente | Tools HTTP endurecidas (SSRF), MCP cliente HTTP, auditoria | F3 | [F3-F9.md](F3-F9.md) |
| F5 Segurança e governança | Auth com escopos/JWT, identidade do principal, rate limit, guardrails, segredos por referência, supply chain | F3, F4 | [F3-F9.md](F3-F9.md) |
| F6 Protocolos | AG-UI 1.0, AgentProtocol, servidor MCP | F3, F5 | [F3-F9.md](F3-F9.md) |
| F7 RAG plugável | VectorStore (Mongo + Chroma), parsers, estratégias (semantic, hierarchical, hybrid) | F2, F4 | [F3-F9.md](F3-F9.md) |
| F8 Observabilidade e Evals | `gen_ai.*`, métricas de token/custo, evals determinísticas | F2 | [F3-F9.md](F3-F9.md) |
| F9 Execução durável | Fila durável, runs em background, cancelamento | F3 | [F3-F9.md](F3-F9.md) |

## Status dos itens
| Item | Status |
|---|---|
| F0-01 | concluído |
| F0-02 | concluído |
| F0-03 | concluído |
| F1-01 … F1-08 | pendente |

## Molde de item (lido pelo `/dev-cycle`)
```
## <id> — <título>
Base: <branch/commit> (opcional)   Branch: <branch> (opcional)
Objetivo: ...
Escopo: ...
Fora de escopo: ...
Critérios de aceite: (verificáveis localmente)
Testes exigidos: ...
Riscos e dependências: ...
```

Referências de diagnóstico (A1-A6, B1-B16, S1-S12) vêm do plano aprovado; as linhas citadas são indicativas.
