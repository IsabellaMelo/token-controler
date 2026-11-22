# token-controler — Documento de design

*Versão 0.1 · 21/11/2026 · Isabella Melo · backlog em `docs/tarefas.md`*

## 1. Visão

Uma ferramenta **local, gratuita e simples** para ver quanto custam e como se comportam as sessões com ferramentas de AI, como o GitHub Copilot e o Claude Code. Ela concentra várias sessões e várias ferramentas num lugar só.

A motivação é ganhar visibilidade sobre o que acontece por trás: não só *quanto* se gastou, mas *por que* uma sessão ficou cara ou lenta.

É uma versão simplificada inspirada no CodeBurn, no ccusage e no AgentTrace, adaptada a um notebook corporativo: Python, sem Node e sem rede. A única dependência externa é o `rich`, usada só no painel do `tc live`; os demais comandos rodam só com a biblioteca padrão.

### Fora do escopo (por enquanto)
- Dashboard web, Power BI, app desktop, extensão do VS Code.
- Dezenas de providers: só entram as ferramentas realmente usadas.
- Ligação com commits e PRs.
- Qualquer envio de dados para fora da máquina.

---

## 2. Princípios

1. **Local e só leitura.** Lê os arquivos que as ferramentas já gravam e nunca os modifica. Zero chamadas de rede.
2. **Nunca guardar texto.** O banco recebe só números e metadados: modelo, tokens, nome da tool, flags de erro e timestamps. Prompts, respostas, resultados e argumentos de tools são descartados na leitura. Única exceção aprovada: um rótulo derivado do prompt (`debug`), nunca o texto (ver `AGENTS.md`, regra 1).
3. **Não estimar a partir do texto.** Na falta de contagem real de tokens, mostrar "sem dados" em vez de um palpite. Essa é a abordagem do ccusage. O CodeBurn estima o Copilot pelo tamanho do texto, mas essas estimativas podem errar por cerca de 2x.
4. **Todo número diz de onde veio.** Cada custo leva uma *origem* e cada sessão leva um *nível de detalhe* (seção 5).
5. **Checklist para aceitar uma fonte** (regra do ccusage): ter timestamp, identificação da sessão, identificação do modelo e contagem de tokens ou custo registrado. Uma ferramenta que só guarda prompts, transcrições ou percentual de cota não entra.
6. **Um adaptador por ferramenta**, isolado do resto. Se o formato de uma ferramenta mudar, quebra só um arquivo.
7. **Schema único.** Todas as fontes gravam nas mesmas tabelas, com a coluna `ferramenta`, e os relatórios não sabem de onde veio o dado.

---

## 3. O que aprendemos com as referências

| Referência | Ponto forte | O que adotamos |
|---|---|---|
| **ccusage** | Contabilidade de custo rigorosa | Checklist de fontes; não estimar pelo texto; modos de custo `auto`, `calculate` e `display`; saída em JSON; config em camadas |
| **CodeBurn** | Amplitude e comportamento | Caminhos do Copilot no VS Code; rótulos de origem do custo; categorias de tarefa determinísticas; taxa de one-shot; histórico diário que sobrevive à limpeza dos logs |
| **AgentTrace** | Diagnóstico do "por quê" | Detecção de anomalias (retry em loop, pausas longas, contexto, leituras repetidas, tools lentas); nível de detalhe por sessão; relatório de confiança dos dados; recomendações com evidência; privacidade por etapas |

Os três têm licença MIT. Podem ser clonados em `../referencias/` (fora deste repo) para consulta.

---

## 4. Fontes de dados

### 4.1 Claude Code (pessoal) — **implementada na Fase 1**
- **Onde:** `~/.claude/projects/**/*.jsonl` (ou `CLAUDE_CONFIG_DIR`), incluindo `subagents/`.
- **O que tem:** tokens por chamada (input, output, gravação de cache com TTL de 5 min ou 1 h, leitura de cache), modelo, `tool_use` e `tool_result` com `is_error`, timestamps, `sessionId`, `cwd`.
- **Cuidados:**
  - O Claude Code grava várias linhas por resposta. A deduplicação usa `message.id` + `requestId` e fica a linha com o maior output.
  - O Claude Code apaga transcrições depois de cerca de 30 dias. Configurar `cleanupPeriodDays` e rodar `tc ingest` com frequência.
  - O formato não é documentado oficialmente.
- **Nível de detalhe:** completo.

### 4.2 GitHub Copilot no VS Code (trabalho) — **Fase 2**
Caminhos no Windows, pela documentação do CodeBurn:
- **Sessões de chat:** `%APPDATA%\Code\User\workspaceStorage\<hash>\chatSessions\*.jsonl` e `globalStorage\emptyWindowChatSessions\*.jsonl`. Versões antigas usam `*.json`.
  - Tokens: `result.metadata.promptTokens` e `result.metadata.outputTokens`. Requisições sem esses campos ficam como "sem dados", não estimadas.
  - Sem divisão de cache.
  - **Nível de detalhe:** agregado.
- **Armazenamento do OpenTelemetry:** `agent-traces.db`, um SQLite do Copilot Chat, se existir. Traz tokens de input, output, leitura e criação de cache por chamada.
  - **Nível de detalhe:** completo.
  - Se ele existir, tem prioridade sobre os `chatSessions`, para não contar em dobro.
- **Custo:** preço de API do modelo, convertido em créditos (1 crédito = US$ 0,01). Desde 01/06/2026, a cobrança do Copilot é por uso: depende do modelo e dos tokens, e o plano inclui uma cota de AI Credits.
- **Pendências:**
  - Confirmar a estrutura real dos `chatSessions` no notebook do trabalho, com `scripts/inspecionar_copilot.py`, que imprime só a estrutura, sem valores.
  - Verificar se `agent-traces.db` existe.
  - As opções `agentDebugLog` e `otel` não apareceram nas configurações; pode ser política da empresa.

### 4.3 Scripts próprios com a API — **Fase 3**
- Envolver as chamadas ao SDK num wrapper que grava o objeto `usage`, a latência, o modelo, o `request_id`, os erros e os retries.
- **Nível de detalhe:** completo.

### 4.4 Fora do escopo
- **App do Claude (claude.ai, desktop, celular):** não expõe tokens por conversa. Não passa no checklist.

---

## 5. Modelo de dados (SQLite)

Arquivo: `~/.token-controler/tracker.db`. Só números e metadados.

| Tabela | Grão | Colunas principais |
|---|---|---|
| `chamadas` | 1 requisição ao modelo | `id`, `ferramenta`, `sessao_id`, `projeto`, `ts`, `modelo`, `input_tokens`, `output_tokens`, `cache_write_5m`, `cache_write_1h`, `cache_read`, `custo_usd`, `subagente`, `erro_api`, `arquivo` |
| `tool_calls` | 1 uso de tool | `id`, `ferramenta`, `sessao_id`, `projeto`, `ts`, `tool`, `erro`, `subagente`, `arquivo` |
| `eventos` | Linha do tempo mínima | `ferramenta`, `sessao_id`, `projeto`, `ts`, `tipo` (`prompt` ou `atividade`), `arquivo` |
| `arquivos` | Controle de ingestão | `caminho`, `ferramenta`, `tamanho`, `mtime`, `processado_em` |

### Colunas a acrescentar
- ✅ `chamadas.origem_custo`: `registrado` (o log trouxe o custo), `calculado` (tokens × tabela de preços) ou `sem_preco` (modelo fora da tabela ou, no modo `display`, sem custo registrado). Chamada de erro de API fica sem origem. Junto vem `chamadas.custo_registrado`, o `costUSD` bruto do log, que permite trocar de modo só com o banco; o modo ativo fica na tabela `config`.
- `sessoes` (nova tabela ou view): `nivel_detalhe` (`completo`, `agregado` ou `limitado`), `categoria`, `inicio`, `fim`, `tempo_ativo`.
- `tool_calls.duracao_ms` e `tool_calls.alvo_hash`: o alvo é o hash do caminho do arquivo editado ou lido, nunca o caminho em texto. Necessário para one-shot e leituras repetidas.
- ✅ Qualidade: em vez de uma tabela `qualidade` por execução, a contagem de linhas ilegíveis fica em `arquivos.linhas_ruins` (por arquivo, para não dobrar ao reprocessar; NULL = ainda não contado) e o resto é calculado do que já está no banco por `tc qualidade`.

### Histórico
O SQLite é o histórico permanente: os dados continuam lá mesmo depois que as ferramentas apagam os logs originais.

---

## 6. Métricas

⭐ = no MVP · ○ = depois

### 6.1 Contabilidade
| | Métrica | Definição |
|---|---|---|
| ⭐ | Sessões, prompts, chamadas | Contagens distintas; chamadas deduplicadas |
| ⭐ | Tokens | input, output, gravação de cache, leitura de cache, input total |
| ⭐ | Taxa de acerto de cache | `cache_read / (input + cache_write + cache_read)` |
| ⭐ | Custo estimado | por projeto, dia, sessão, modelo e ferramenta, com a origem do custo |
| ⭐ | Tool calls e taxa de erro | total e por tool |
| ⭐ | Erros de API | por código (429, 529, 500, 504...) |
| ⭐ | Duração e tempo ativo | tempo ativo desconta pausas acima de 5 min |
| ○ | Chamadas por prompt | trabalho autônomo por pedido |
| ○ | Custo por prompt | eficiência geral |
| ○ | Economia com cache | custo sem cache − custo real |

### 6.2 Comportamento (inspirado no CodeBurn)
| | Métrica | Definição |
|---|---|---|
| ✅ | Categoria da sessão | Regra determinística, sem chamar modelo, por turno e depois por sessão. Edit/Write → Codificação; Read/Grep sem edição → Exploração; pytest no Bash → Testes; palavras de erro + tools → Debug; subagente → Delegação; sem tools → Conversa. O Debug usa um rótulo derivado do prompt na leitura (exceção aprovada, só o rótulo é gravado) e refina apenas turnos de codificação ou exploração. Feature, Refatoração e Brainstorm não existem. |
| ✅ | Taxa de one-shot | % de turnos de edição sem retry. Retry = o mesmo arquivo editado de novo depois de um comando no shell que não seja só de leitura |

### 6.3 Diagnóstico (inspirado no AgentTrace)
Os limites abaixo são **propostas iniciais**, a calibrar com dados reais. ✅ Todos os sinais estão em `tc diagnostico` (T16), com os limites em `config.json`; a pressão de contexto só roda com as janelas informadas, e "re-criação de cache" exige uma pausa maior que 5 min antes da chamada.

| | Sinal | Regra proposta |
|---|---|---|
| ○ | Cache baixo | taxa de cache < 80% numa sessão com mais de 10 chamadas |
| ○ | Re-criação de cache | chamada no meio da sessão com `cache_write` > 10k tokens (indica pausa ou mudança de contexto) |
| ○ | Loop de retry | a mesma tool falha 3 ou mais vezes seguidas |
| ○ | Pausa longa | intervalo > 5 min entre eventos (cache expirado) |
| ○ | Pressão de contexto | input total de uma chamada acima de X% da janela do modelo |
| ○ | Leitura repetida | o mesmo `alvo_hash` lido 3 ou mais vezes na sessão |
| ○ | Tool lenta | duração acima do p95 daquela tool |
| ○ | Sessão cara | custo acima do p90 das sessões do projeto |

Cada sinal vira uma **recomendação com evidência**, ordenada por impacto estimado. Exemplo: "Sessão a1b2 · cache 41% · 4 re-criações após pausas > 5 min → evite pausas longas ou abra uma sessão nova."

---

## 7. Custo

- **Tabela de preços local** (`precos.py`), atualizada à mão. Sem download automático, porque o notebook não acessa a rede.
- **Modos de custo** (do ccusage):
  - `auto`: usa o custo registrado no log quando existe; senão, calcula.
  - `calculate`: sempre calcula pelos tokens.
  - `display`: só usa o custo registrado.
- **Copilot:** exibir em créditos e em dólares, com 1 crédito = US$ 0,01.
- **Assinaturas (Pro/Max):** o valor é o equivalente de API, não o que se paga de fato. O relatório deve deixar isso claro.
- Modelos sem preço aparecem como `sem_preco` e marcados com `*`. Nunca chutar um valor.

---

## 8. Interface

CLI `tc` (comandos em português):

| Comando | Para quê | Estado |
|---|---|---|
| `tc ingest [--tudo] [--modo auto\|calculate\|display]` | lê logs novos ou alterados; `--modo` troca o modo de custo e recalcula o banco | ✅ |
| `tc resumo --por projeto\|dia\|sessao\|modelo\|ferramenta` | contabilidade agrupada | ✅ |
| `tc sessoes` | duração, tempo ativo, prompts, chamadas, custo | ✅ |
| `tc tools` | uso e taxa de erro por tool | ✅ |
| `tc live [--periodo] [--simples]` | painel visual (rich) atualizado sozinho: resumo, abas de período e uma grade de caixas; `--simples` mantém a tabela antiga | ✅ |
| `tc diagnostico [--sessao] [--limite] [--json]` | sinais e recomendações, cada uma com a evidência | ✅ |
| `tc comportamento [--json]` | categoria dos turnos, one-shot e custo por categoria | ✅ |
| `tc qualidade [--ferramenta] [--json]` | confiança dos dados (linhas ruins, sem preço, sem tokens, origem do custo) | ✅ (nível de detalhe entra com a T09) |
| `--json` em todos | análise no pandas | Fase 1.5 |

**Aba no VS Code:** `Ctrl+Shift+P` → *Terminal: Create Terminal in Editor Area* → `tc live`.

**Configuração** (prioridade, como no ccusage): argumentos do CLI > `~/.token-controler/config.json` > padrões. Apelidos de projeto em `projetos.json`.

---

## 9. Privacidade e segurança

- Nenhum texto de conversa vai para o banco, inclusive no Copilot do trabalho.
- Hash, não caminho, quando for preciso identificar arquivos (one-shot, leituras repetidas).
- Nenhuma chamada de rede, nem para buscar preços.
- O banco é um arquivo local. Não sincronizar com nuvem pessoal nem commitar.
- Antes de usar no trabalho, confirmar com a área de segurança. A ferramenta só lê arquivos que já existem no notebook.

---

## 10. Fases

| Fase | Entrega | Estado |
|---|---|---|
| **1** | SQLite + adaptador Claude Code + CLI (`ingest`, `resumo`, `sessoes`, `tools`, `live`) + testes e CI | ✅ entregue, testada com logs sintéticos |
| **1.5** | Validação com dados reais + `--json` | Próxima |
| **2** | Adaptador Copilot VS Code (`chatSessions` e `agent-traces.db`) + `origem_custo` + `nivel_detalhe` + `tc qualidade` | Depende da estrutura real dos arquivos |
| **3** | Wrapper para scripts com a API (erros, retries, latência) | — |
| **4** | Categorias de tarefa, one-shot e `tc diagnostico` com recomendações | — |
| **5** | Calibrar os limites com 2 a 3 semanas de uso real | — |

**Critério de sucesso:** depois de 2 a 3 semanas, você consulta o `tc` com frequência e ele já te fez mudar algum hábito, como trocar de modelo, abrir sessões novas ou evitar pausas longas.

---

## 11. Questões em aberto

1. Qual é a estrutura real dos `chatSessions` do Copilot no notebook do trabalho, e o `agent-traces.db` existe?
2. A TI permite rodar scripts Python que leem `%APPDATA%\Code`?
3. Qual tabela de preços usar para os modelos do Copilot que não são da Anthropic (GPT, Gemini)?
4. Os números da Fase 1 batem com o `/cost` do Claude Code em uma sessão real?

---

## Referências
- CodeBurn — https://github.com/getagentseal/codeburn (docs: `docs/how-it-works.md`, `docs/providers/copilot.md`)
- ccusage — https://ccusage.com/guide/ (Cost Calculation Modes, Source Support Q&A)
- AgentTrace — https://github.com/luoyuctl/agenttrace
- Preços da Anthropic — https://platform.claude.com/docs/en/about-claude/pricing
- Prompt caching — https://platform.claude.com/docs/en/build-with-claude/prompt-caching
- Erros da API — https://platform.claude.com/docs/en/api/errors
- Copilot OpenTelemetry (VS Code) — https://code.visualstudio.com/docs/copilot/guides/monitoring-agents
