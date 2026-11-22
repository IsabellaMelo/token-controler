# Formato dos logs do Claude Code (só estrutura)

Confirmado na T21 com `scripts/inspecionar_claude_code.py` em 22/11/2026: 12 arquivos (9 sessões e 3 subagentes), 7 721 linhas.
Este arquivo tem **campos, tipos e contagens, nunca valores**. Nada aqui vem de texto de conversa. Para rodar de novo:
`python scripts/inspecionar_claude_code.py` (a saída também só tem estrutura). O formato muda entre versões do Claude Code:
se uma conta deixar de bater, rode o script antes de mexer no adaptador.

## Arquivos
- `~/.claude/projects/<projeto>/<sessao>.jsonl`: a sessão (thread principal).
- `~/.claude/projects/<projeto>/<sessao>/subagents/agent-<agentId>.jsonl`: um arquivo por subagente, com
  `agent-<agentId>.meta.json` ao lado (chaves: `agentType`, `isBuiltIn`, `description`, `toolUseId`, `spawnDepth`, `requestShape`,
  `requestNonInteractive`, `pickedEffortModel`, `finished`).

## Tipos de linha (`type`)
`assistant`, `user`, `attachment`, `system` (subtype `compact_boundary`) e vários de bookkeeping: `api-request`,
`api-request-blob`, `api-request-shape`, `session-owner`, `atis-latch`, `last-prompt`, `ai-title`, `data-flow-records`,
`queue-operation`, `file-history-delta`, `file-history-snapshot`, `tools-used`, `cost-state`, `derived-from`, `mode`.
O adaptador só lê `user`, `assistant` e `system`.

**Cuidado:** `api-request-blob` guarda **cópias das mensagens** (`message` com blocos `tool_use`/`tool_result`), sem `uuid` nem
`timestamp`. Contar esses blocos dobraria as tools.

## Ligações confirmadas
| O que | Como | Confirmação |
|---|---|---|
| `tool_use` → `tool_result` | `tool_result.tool_use_id == tool_use.id`, sempre no mesmo arquivo | 1008 de 1008 pares; nenhum `tool_use_id` com dois resultados |
| Resultado → linha do `tool_use` | `parentUuid` do resultado é a linha `assistant` do `tool_use`; também `sourceToolAssistantUUID` | 1008 de 1008 e 1001 de 1001 |
| Uma resposta em várias linhas | uma linha `assistant` por bloco, todas com o mesmo `message.id` e `requestId` | 586 de 741 mensagens |
| Turno | `promptId` (nas linhas `user`) é herdado por todos os `tool_result` do turno; só troca num prompt novo, `isMeta` ou resumo de compactação. `turnPosition{promptIndex, turnIndex}` existe nas linhas de prompt. O `tc` conta os prompts com `_eh_prompt` (mesma regra do `comportamento.py`) | 1001 de 1001 |
| Chamada `Agent` → subagente | `toolUseResult.agentId` na linha do resultado; e `toolUseId` no `agent-<id>.meta.json`; o `agentId` também é o nome do arquivo | 3 de 3, pelos dois lados |
| Linhas do subagente | `isSidechain: true`, `sessionId` da sessão pai, primeira linha com `parentUuid` nulo | 3 de 3 |

A tool de subagente se chama **`Agent`** (versões antigas: `Task`).

## `tool_result`
Bloco: `type`, `tool_use_id`, `content` (texto nos erros), `is_error` (só quando é erro: `true`; ausente ou `false` nos demais).
Na linha `user` do resultado: `toolUseResult` (varia por tool), `permissionDecision` (objeto), `toolDenialKind` (só em negações) e
`sourceToolAssistantUUID`.

- **Erros:** 47 em 1008 resultados; `content` sempre texto; mediana de 573 caracteres e máximo de 10 040. Em Bash e PowerShell o texto
  começa com `Exit code N` (34 de 37); os demais são mensagens do próprio Claude Code (`<tool_use_error>…`).
- **Recusa e bloqueio:** `toolDenialKind` com valores `user-rejected` (usuário negou), `permission-rule` e `automode-blocked`
  (bloqueio por regra ou classificador). Só aparece nas linhas negadas.
- **Exit code:** não há campo estruturado. `toolUseResult` do Bash/PowerShell tem `stdout`, `stderr`, `interrupted`, `isImage`,
  `noOutputExpected`, `timedOutAfterMs` (raro) e `returnCodeInterpretation` (raro).
- **Duração própria:** só `Glob.durationMs`, `WebSearch.durationSeconds` e `Agent.totalDurationMs`. Para as demais tools a única
  medida é a diferença de timestamps entre a linha do `tool_use` e a do `tool_result`, que **inclui a espera pela aprovação**.
- **MCP:** nome `mcp__<servidor>__<tool>`. A amostra não tinha nenhuma chamada MCP: o formato do erro de autenticação de MCP é
  coberto só por fixture sintética.

## O que o `tc` guarda a partir disso
`tool_calls.turno`, `seq`, `chamada_id`, `subagente_id`, `servidor_mcp`, `exit_code`, `erro_categoria` e, em `chamadas`, `turno`,
`seq` e `subagente_id` (design §5 e §6.4). A mensagem de erro é lida só em memória por `token_controler/regras_erros.py`.
