# token-controler — Documento de design

*Versão 0.1 · 21/11/2026 · backlog em `docs/tarefas.md` · atualizado em 22/11/2026 (`tc web`, sequência da sessão e categorias de erro: T21 a T29)*

## 1. Visão

Uma ferramenta **local, gratuita e simples** para ver quanto custam e como se comportam as sessões com ferramentas de AI, como o GitHub Copilot e o Claude Code. Ela concentra várias sessões e várias ferramentas num lugar só.

A motivação é ganhar visibilidade sobre o que acontece por trás: não só *quanto* se gastou, mas *por que* uma sessão ficou cara ou lenta.

É uma versão simplificada inspirada no CodeBurn, no ccusage e no AgentTrace, adaptada a um notebook corporativo: Python, sem Node e sem rede. A única dependência externa é o `rich`, usada só nas telas visuais (`tc painel`, `tc live` e `tc sessao`); os demais comandos rodam só com a biblioteca padrão. O `tc web` (Fase 2.5) não usa nem o `rich`: é HTML e CSS gerados em Python, sem JavaScript.

### Fora do escopo (por enquanto)
- ~~Dashboard web~~ (passou a ser o `tc web`: local, só em `127.0.0.1`, sem JavaScript; ver 8.1), Power BI, app desktop, extensão do VS Code.
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

### 4.3 Scripts próprios com a API — ~~Fase 3~~ **fora do backlog atual** (a T12 foi retirada de `docs/tarefas.md`; o texto abaixo fica como referência)
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
- ✅ (T13) `tool_calls.duracao_ms` e `tool_calls.alvo_hash`: o alvo é o hash do caminho do arquivo editado ou lido, nunca o caminho em texto. Necessário para one-shot e leituras repetidas.
- ✅ Qualidade: em vez de uma tabela `qualidade` por execução, a contagem de linhas ilegíveis fica em `arquivos.linhas_ruins` (por arquivo, para não dobrar ao reprocessar; NULL = ainda não contado) e o resto é calculado do que já está no banco por `tc qualidade`.

### Colunas novas para o `tc web` e as categorias de erro (Fase 2, tarefas T22 e T23)
A tela de sequência do `tc web` (8.1) precisa saber a ordem dos passos, qual chamada ao modelo pediu cada tool e qual
subagente respondeu a uma chamada `Task`. Hoje isso é deduzido por horário (`detalhe.py`); passa a ser gravado.
Continua valendo a regra: só números, IDs e metadados, nunca texto (ver 6.4 para as duas colunas de erro).

| Coluna | Tabela | O que guarda |
|---|---|---|
| `turno` | `chamadas`, `tool_calls` | Número do turno na sessão. **1 turno = 1 prompt + tudo até a resposta** (a mesma definição do `comportamento.py`). Contagem a partir de 1, na ordem dos prompts; atividade antes do primeiro prompt fica no turno 0. O subagente herda o turno da chamada `Agent` que o abriu. *Decidido e implementado na T22.* |
| `seq` | `chamadas`, `tool_calls` | Ordem dentro da sessão: inteiro crescente na ordem em que o passo aparece no log. Uma sequência só por sessão (1..n), compartilhada pelas duas tabelas e ordenada por horário: a chamada vem antes das tools que pediu e o subagente entra no meio da thread principal. *Decidido e implementado na T22.* |
| `chamada_id` | `tool_calls` | Qual chamada ao modelo (`chamadas.id`) pediu a tool, ligada por `tool_use_id`. Substitui a atribuição por horário que o `tc sessao` usa hoje para dividir o custo entre tools. |
| `subagente_id` | `tool_calls`, `chamadas` | Liga a chamada `Agent` (no Claude Code atual a tool se chama `Agent`; versões antigas `Task`) às chamadas e tools do subagente que ela abriu. É o `agentId`: na chamada `Agent`, o subagente que ela abriu (vem do `toolUseResult.agentId`, com reserva no `toolUseId` do `.meta.json`); nas chamadas e tools do subagente, o próprio `agentId` (= nome do arquivo `agent-<id>.jsonl`). **Confirmado na T21**: bate em 3 de 3 casos pelos dois lados, sem heurística de horário. Fica nas duas tabelas. |
| `servidor_mcp` | `tool_calls` | Servidor MCP da tool: `mcp__google-drive__search_files` → servidor `google-drive`, tool `search_files`. NULL para tools que não são MCP. |
| `duracao_ms` | `tool_calls` | **Já existe** (T13): diferença de timestamps entre a linha do `tool_use` e a do `tool_result`. **É "tempo até o resultado"**: inclui a espera pela aprovação do usuário (T21: só `Glob`, `WebSearch` e `Agent` trazem duração própria; as demais não). A barra de início e duração da sequência usa esse valor. |
| `exit_code` | `tool_calls` | Código de saída de um comando de shell (`Bash`, `PowerShell`) que falhou. **Confirmado na T21**: não existe campo estruturado; o número vem do início da mensagem de erro (`Exit code N`). Sucesso e tools que não são de shell ficam NULL (o sucesso é 0 por dedução, não gravado). Só o número. |
| `erro_categoria` | `tool_calls` | Categoria do erro quando a tool falha (ver 6.4). Só o rótulo, nunca a mensagem. NULL = sem erro, ou ainda não classificado (`tc ingest --tudo`). |
| `alvo_ext` | `tool_calls` | Extensão do arquivo alvo, ao lado do `alvo_hash`. **PENDENTE DE DECISÃO: não implementar.** |

**Nome da tool de subagente (T21):** o Claude Code atual chama a tool de `Agent`; o código trata `Agent` e `Task`.
**Linhas `api-request-blob` (T21):** o log traz cópias das mensagens nesse tipo de linha, sem `uuid` nem `timestamp`.
O adaptador só lê `user` e `assistant`; contar blocos de `api-request-blob` dobraria `tool_use` e `tool_result`.

O `tc ingest --tudo` repovoa as colunas novas das sessões cujo log ainda existe; as demais ficam NULL e as telas mostram
"sem dados" em vez de inventar.

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

*Nota (Fase 2):* as tools recusadas pelo usuário (`recusado_usuario`, ver 6.4) deixam de contar como erro na taxa de erro.

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
| ○ | Tool lenta | duração acima do p95 daquela tool. **Ignora as tools que passaram por aprovação do usuário**: a duração inclui a espera pela resposta (ver 6.5) |
| ○ | Sessão cara | custo acima do p90 das sessões do projeto |
| ○ | Loop de retry por categoria *(Fase 3, T29)* | o loop de retry acima, quando as falhas da corrida têm a mesma `erro_categoria`: usa a recomendação da categoria (6.4) |
| ○ | Erro recorrente *(novo, Fase 3, T29)* | a mesma `erro_categoria` em 3 ou mais sessões do projeto em 7 dias. **Não conta** `excecao_codigo`, `recusado_usuario` nem `outro` (`regras_erros.conta_para_erro_recorrente`) |

Cada sinal vira uma **recomendação com evidência**, ordenada por impacto estimado. Exemplo: "Sessão a1b2 · cache 41% · 4 re-criações após pausas > 5 min → evite pausas longas ou abra uma sessão nova."

### 6.4 Categorias de erro (Fase 2 grava, Fase 3 diagnostica)
Quando uma tool falha, o `tc` lê a **mensagem de erro do `tool_result` só em memória**, aplica regras determinísticas
(regex, em português e inglês, para Windows e Unix) e grava **apenas** `tool_calls.erro_categoria` e `tool_calls.exit_code`.
A mensagem não é gravada, impressa nem logada, como no rótulo `debug` do prompt (6.2). *Esta é uma nova exceção à regra 1
do `AGENTS.md` (decisão do dono do projeto): ela precisa ser registrada lá antes da implementação (T23).*

As regras ficam em `token_controler/regras_erros.py`, com **um teste por categoria** (fixtures sintéticas, nunca mensagens
reais). **Testar na ordem da tabela** (`recusado_usuario`, `bloqueado_permissao` e `mcp_auth` primeiro; as duas primeiras
vêm do campo `toolDenialKind`, sem ler texto; o `mcp_auth` só vale para tool de servidor MCP; `edicao_invalida` só para `Edit`/`Write`), porque uma recusa ou um 401 de MCP também
poderia casar com outra categoria. `codificacao_console` vem antes de `excecao_codigo`, que fica por último antes de `outro`: um
Traceback só é `excecao_codigo` se nenhuma outra categoria o explica. O `tc ingest --tudo` reclassifica (só enquanto o log existir).
As 11 categorias do primeiro desenho viraram 13 na T21 e 15 na rodada seguinte, ambas decididas pelo dono do projeto.

*Validação nos logs reais (T21, 47 erros):* com as 11 categorias originais, 21 erros (45%) caíam em `outro`. Entre eles havia
3 bloqueios de permissão (hoje `bloqueado_permissao`) e 3 falhas de `Edit`/`Write` (hoje `edicao_invalida`). `mcp_auth` e
`rede` não tiveram nenhum caso real nesta amostra: são testados só com fixtures. O percentual em `outro` depois das regras
novas é medido na T23 e registrado na tarefa. Sobraram em `outro`, por decisão do dono do projeto, os `fatal:` do git.

| Categoria | Sinais | Recomendação |
|---|---|---|
| `recusado_usuario` | campo `toolDenialKind == "user-rejected"` na linha do resultado (T21: dispensa ler o texto) | NÃO conta como erro na taxa nem nos loops |
| `bloqueado_permissao` | `toolDenialKind` = `permission-rule` ou `automode-blocked`: bloqueio por regra ou classificador, não escolha do usuário | Se o bloqueio não era esperado, ajuste as regras de permissão do projeto (`settings.json`) |
| `mcp_auth` | erro de MCP com 401, unauthorized, token expired | Reautentique o servidor MCP antes de começar |
| `modulo_ausente` | ModuleNotFoundError, No module named, ImportError | Registre no CLAUDE.md como ativar o ambiente |
| `comando_ausente` | command not found, is not recognized as an internal or external command, não é reconhecido | Registre os comandos do projeto no CLAUDE.md; confira o PATH |
| `sintaxe_shell` | syntax error, ParserError, Unexpected token | Diga no CLAUDE.md qual shell usa, com exemplos |
| `codificacao_console` | UnicodeEncodeError, UnicodeDecodeError, `charmap`, cp1252, codec can't encode/decode (saída do console do Windows) | Use `PYTHONUTF8=1` ou registre no CLAUDE.md que a saída deve ser UTF-8 |
| `edicao_invalida` | só em `Edit`/`Write`: String to replace not found, File has not been read yet, File has been modified since read | Leia o arquivo antes de editar e peça trechos únicos e menores |
| `arquivo_ausente` | No such file or directory, FileNotFoundError, cannot find the path | Abra a sessão na raiz e descreva a estrutura no CLAUDE.md |
| `permissao` | Permission denied, Access is denied, Acesso negado, EACCES | Ajuste a permissão ou fale com a TI |
| `timeout` | timed out | Rode só os testes relevantes |
| `teste_falhou` | FAILED, AssertionError, N failed | Normal se o código muda entre tentativas; senão é loop |
| `rede` | Connection refused, ConnectTimeout, proxy | Evite pedir instalações ou downloads |
| `excecao_codigo` | Traceback do Python (e `Exception in thread`, `panic:`, `Uncaught Error`, `Unhandled exception`) que nenhuma categoria acima explica: erro do código que está sendo escrito. **Não grava o nome da exceção**, só o rótulo | Leia o traceback e corrija a causa antes de repetir; se nada mudou entre as tentativas, é loop. Conta para o loop de retry, **não** para o erro recorrente |
| `outro` | nada acima (por exemplo `fatal:` do git, processo morto) | Aparece no `tc qualidade` para melhorar as regras |

- **`recusado_usuario` não é erro de verdade:** fica fora da taxa de erro (`tc tools`, `tc resumo`, painéis) e fora da
  contagem de falhas seguidas dos loops.
- **`tc qualidade`** passa a contar os erros que caíram em `outro`: quanto maior, mais regras faltam.
- **Diagnóstico (Fase 3, tarefa T29):**
  - **Loop de retry:** a mesma tool falha 3 ou mais vezes seguidas (6.3). Se todas as falhas da corrida têm a mesma
    categoria, o sinal usa a recomendação dela em vez da genérica.
  - **Erro recorrente (novo):** a mesma categoria aparece em 3 ou mais sessões do projeto em 7 dias. A recomendação é a da
    categoria, e costuma pedir uma mudança no `CLAUDE.md` do projeto.

### 6.5 Duração de tool e aprovação do usuário
`tool_calls.duracao_ms` é a diferença de timestamps entre a linha do `tool_use` e a do `tool_result`. Quando a tool precisa de
aprovação, esse tempo **inclui a espera da pessoa** (T21: `AskUserQuestion` com mediana de 80 s e máximo de 22 min).

**O log não registra quando a aprovação foi pedida.** Conferido em 22/11/2026 em todos os tipos de linha: o `permissionDecision` da
linha do resultado só tem `decision`, `source`, `reasonType`, `reasonCode` e `grantScope`, sem horário; o `queue-operation` trata
a fila de prompts, não de permissões; nenhum `attachment` marca o pedido. Por isso **não existe `espera_aprovacao_ms`** e não dá
para separar a espera do tempo de execução.

O que existe é o **`permissionDecision.source`**: `config` quando a decisão foi automática (modo, classificador ou regra:
1 068 de 1 082 resultados) e `user_temporary` quando a pessoa aprovou na hora (14 casos, todos `AskUserQuestion`). Regra do
diagnóstico: **o sinal "tool lenta" ignora as tools que passaram por aprovação do usuário** (`source` diferente de `config`), além
das tools da lista `tools_que_esperam`, que já são ignoradas. Hoje isso não está implementado e o ingest **não grava** o `source`:
implementar pede uma coluna nova (proposta: `tool_calls.aprovacao` = `automatica` ou `usuario`), que é a questão 11 do §11.
A amostra só tem aprovações do usuário em `AskUserQuestion`; aprovações manuais de `Bash` ou `Edit` (modo padrão do Claude
Code) não apareceram e o valor de `source` nelas é uma hipótese a validar.

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
| `tc live [--projeto] [--simples]` | tela AGORA (rich), atualizada sozinha: sessões ativas (últimos 15 min) com branch, custo, custo/hora e erros; alertas com o `tc sessao <id>`; custo de hoje por projeto e por hora; blocos sem dados somem (T20) | ✅ |
| `tc painel [--periodo] [--simples]` | painel por período (rich): resumo, abas de período e uma grade de caixas; `--simples` mantém a tabela antiga (era o `tc live` até a T20) | ✅ |
| `tc diagnostico [--sessao] [--limite] [--json]` | sinais e recomendações, cada uma com a evidência | ✅ |
| `tc sessao [id] [--comandos] [--json]` | detalhe de uma sessão em árvore (`rich.Tree`): prompts, tools, subagentes, pausas e ⚠ com motivo e custo (T19) | ✅ |
| `tc limpar-nomes` | apaga os nomes de arquivo gravados em `tool_calls.alvo_nome` (T19, em teste) | ✅ |
| `tc limpar-trechos` | apaga os trechos de leitura (`leitura_ini`, `leitura_qtd`) gravados (T19, em teste) | ✅ |
| `tc comportamento [--json]` | categoria dos turnos, one-shot e custo por categoria | ✅ |
| `tc qualidade [--ferramenta] [--json]` | confiança dos dados (linhas ruins, sem preço, sem tokens, origem do custo) | ✅ (nível de detalhe entra com a T09; a contagem de erros em `outro` entra com a T23) |
| `tc web [--porta N]` | painel local no navegador: Hoje, detalhe da sessão e sequência da sessão (8.1) | Hoje ✅ (T24, `--demo` para testes); T25 a T28 pendentes |
| `--json` em todos | análise no pandas | Fase 1.5 |

**Aba no VS Code:** `Ctrl+Shift+P` → *Terminal: Create Terminal in Editor Area* → `tc live` ou `tc painel`.

### 8.1 `tc web`: painel local no navegador (Fase 2.5)
Uma visão em páginas HTML, para ver sem terminal e para gráficos que o terminal não faz bem. Decisões:

- **Sem JavaScript e sem bibliotecas.** Python gera HTML e CSS no servidor (só biblioteca padrão; nem o `rich`).
- **Servidor:** `http.server.ThreadingHTTPServer`, ligado **só em `127.0.0.1`**. A porta é configurável (`--porta`); se
  estiver ocupada, tenta a próxima.
- **Banco:** as conexões do servidor abrem o SQLite em **modo só leitura** (`mode=ro`). O banco passa a usar **WAL**, para o
  ingest gravar sem travar as leituras. Consequências a tratar na implementação: arquivos `-wal` e `-shm` ao lado do banco
  (entram no `.gitignore`) e validar no Windows a abertura `mode=ro` de um banco em WAL.
- **"Ao vivo":** `<meta http-equiv="refresh" content="5">` recarrega a página. Uma thread roda o `ingest` incremental no
  mesmo intervalo, com a própria conexão de escrita.
- **Estado na URL**, para favoritar e voltar: `/?periodo=7d&projeto=x`, `/sessao/<id>`,
  `/sessao/<id>/sequencia?turno=3&tipo=erros`.
- **Gráficos:** barras em `div` com largura em % ou px; rosca com `conic-gradient`; linhas com `<polyline>` em SVG; tooltip
  com o atributo `title`. Fontes do sistema (Segoe UI, Cascadia Mono ou Consolas).

**Três telas**

1. **Hoje**
   - 6 KPIs: custo (com sparkline), tokens, taxa de cache, chamadas, tool calls e erros, tempo ativo.
   - Gasto por hora; custo por tipo de token (rosca); por projeto, por modelo e por tool.
   - Tabela de sessões, com os sinais do diagnóstico ao lado de cada uma.
   - A etapa seguinte acrescenta as abas de período e os filtros (`periodo`, `projeto`).
2. **Detalhe da sessão** (`/sessao/<id>`)
   - KPIs da sessão.
   - Chamadas por minuto (linha), com marcas para pausas, re-criações de cache, loops e prompts.
   - Diagnóstico com as recomendações (as mesmas do `tc diagnostico`).
   - Tokens e tools.
3. **Sequência da sessão** (`/sessao/<id>/sequencia`)
   - **Turnos à esquerda**; no centro, os **passos do turno em ordem**: prompt, modelo, leitura, escrita, shell, MCP,
     subagente. Cada passo mostra horário, detalhe, duração, custo e uma **barra de início e duração dentro do turno**.
   - **Loops de retry agrupados** num bloco só (a mesma tool falhando 3 ou mais vezes seguidas).
   - **Filtros** por tipo de passo e "só erros" (`tipo=erros`).
   - O "detalhe" de um passo é o que já é permitido gravar: nome do arquivo e trecho lido, classe do comando de shell,
     servidor e tool do MCP, categoria do erro. **Prompts e comandos de shell não aparecem em texto** (como no `tc sessao`).
   - Depende das colunas novas da seção 5 (`turno`, `seq`, `chamada_id`, `subagente_id`, `servidor_mcp`).

**Cuidados de segurança (propostas, a confirmar):** todo texto vindo do banco (projeto, nome de arquivo, branch) passa por
`html.escape`; só `GET`, sem formulários; checar o cabeçalho `Host` (contra DNS rebinding); sem CORS; sem log de requisição
com dados da sessão.

**Configuração** (prioridade, como no ccusage): argumentos do CLI > `~/.token-controler/config.json` > padrões. Apelidos de projeto em `projetos.json`.

**Decisões da implementação (T24, 22/11/2026):**
- Porta padrão 8765 (`--porta` > `web_porta` no config.json > 8765); ocupada → até 20 portas seguintes. No Windows o
  socket usa `SO_EXCLUSIVEADDRUSE` (sem isso, `SO_REUSEADDR` deixaria dois servidores na mesma porta).
- Cabeçalhos: `Content-Security-Policy: default-src 'none'; style-src 'self' 'unsafe-inline'; img-src 'self' data:` (sem
  script, sem recurso externo), `nosniff`, `no-referrer`, `no-store`; sem CORS; `log_message` desligado. `Host` aceito:
  `127.0.0.1:<porta>` e `localhost:<porta>`; outro → 403. Método que não é `GET` → 405.
- CSS numa folha comum (`/estilo.css`) com classes; `style=` só para a geometria calculada (largura, altura, fatias da
  rosca), por isso o `'unsafe-inline'` só em `style-src`. Modelos HTML em `token_controler/web/modelos/` (`string.Template`);
  todo valor entra escapado, salvo trechos montados pelo próprio `telas.py`.
- Banco: o servidor liga o WAL e abre uma conexão de escrita só para a thread de ingest (5 s, com trava contra duas
  rodadas); cada requisição lê com `mode=ro` (validado no Windows: lê com o WAL aberto, escrita falha).
- `tc web --demo`: banco fictício gerado por `token_controler/demo.py` (horários no fuso local, semente fixa), "agora"
  congelado em 22/11/2026 11:07:42, sem ingest e sem `<meta refresh>`. É a base dos testes visuais (Playwright, só
  desenvolvimento, `tests/visual/`).
- Tela Hoje: sinais na tabela só de severidade média e alta, num selo só (os de baixa ficam para o detalhe da sessão);
  "custo por tipo de token" só divide o custo `calculado` (o `registrado` aparece numa fatia "sem divisão"); "sem cache
  custaria" = os mesmos tokens com o contexto inteiro cobrado como input. As abas de período ficam visíveis mas desligadas
  até a T25, e `/sessao/<id>` mostra um aviso até a T26.

---

## 9. Privacidade e segurança

- Nenhum texto de conversa vai para o banco, inclusive no Copilot do trabalho.
- Hash, não caminho, quando for preciso identificar arquivos (one-shot, leituras repetidas). Exceção em teste (T19, 22/11/2026):
  o **nome** do arquivo, sem pasta, em `tool_calls.alvo_nome` (desligável por `gravar_nome_arquivo`; `tc limpar-nomes` apaga).
  Comando de shell só na tela, com `tc sessao --comandos`, lido do log na hora e nunca gravado.
  Segunda exceção em teste (T19, 22/11/2026): nos `Read`, os dois números do trecho lido (`offset` e `limit`) em
  `leitura_ini`/`leitura_qtd`, para não confundir ler outro trecho com reler (desligável por `gravar_trecho_leitura`; `tc limpar-trechos` apaga).
- Nenhuma chamada de rede, nem para buscar preços. O `tc web` só **escuta** em `127.0.0.1`: não faz conexão de saída e não é
  alcançável de outra máquina. *A regra 2 do `AGENTS.md` ("nunca fazer chamada de rede") passou a dizer isso explicitamente (22/11/2026).*
- Mensagem de erro de tool: lida só em memória para escolher a categoria; só `erro_categoria` e `exit_code` vão ao banco (6.4).
  *Nova exceção à regra 1 do `AGENTS.md`, a registrar na T23.*
- O banco é um arquivo local. Não sincronizar com nuvem pessoal nem commitar.
- Antes de usar no trabalho, confirmar com a área de segurança. A ferramenta só lê arquivos que já existem no notebook.

---

## 10. Fases

| Fase | Entrega | Estado |
|---|---|---|
| **1** | SQLite + adaptador Claude Code + CLI (`ingest`, `resumo`, `sessoes`, `tools`, `live`) + testes e CI | ✅ entregue, testada com logs sintéticos |
| **1.5** | Validação com dados reais + `--json`; **além disso**, confirmar nos logs reais `tool_use_id`, `parentUuid`, a ligação com o subagente e o texto de erro dos `tool_result` (T21) | T01, T02 e T21 ✅ |
| **2** | Adaptador Copilot VS Code (`chatSessions` e `agent-traces.db`) + `origem_custo` + `nivel_detalhe` + `tc qualidade`; **além disso**, o `ingest` grava os campos da seção 5 (T22) e as categorias de erro da 6.4 (T23) | Copilot depende da estrutura real dos arquivos; T22 e T23 ✅ |
| **2.5** | `tc web` (8.1), em quatro passos: Hoje → abas e filtros → detalhe da sessão → sequência (T24 a T28) | Pendente |
| **3** | Categorias de tarefa, one-shot e `tc diagnostico` com recomendações; **além disso**, os sinais de loop por categoria e de erro recorrente (T29) | T13 a T16, T19 e T20 ✅; T29 pendente |
| **4** | Calibrar os limites com 2 a 3 semanas de uso real | — |
| — | Wrapper para scripts com a API (era a Fase 3; ver 4.3) | retirado do backlog |

*Numeração alinhada ao `docs/tarefas.md`, que perdeu a antiga Fase 3 (T12): o diagnóstico passou de Fase 4 para 3 e a calibração de 5 para 4.*

**Critério de sucesso:** depois de 2 a 3 semanas, você consulta o `tc` com frequência e ele já te fez mudar algum hábito, como trocar de modelo, abrir sessões novas ou evitar pausas longas.

---

## 11. Questões em aberto

1. Qual é a estrutura real dos `chatSessions` do Copilot no notebook do trabalho, e o `agent-traces.db` existe?
2. A TI permite rodar scripts Python que leem `%APPDATA%\Code`?
3. Qual tabela de preços usar para os modelos do Copilot que não são da Anthropic (GPT, Gemini)?
4. ~~Os números da Fase 1 batem com o `/cost` do Claude Code em uma sessão real?~~ **Validado** (22/11/2026) contra o
   ccusage 20.0.51 (`ccusage claude daily --offline --breakdown`), por dia × modelo: input, output, gravação de cache (5 min
   e 1 h), leitura de cache e custo batem em 0,000% nos dias fechados (15, 20 e 21/11) e, numa cópia congelada dos logs,
   também no dia em andamento. O `ccusage daily` sem `claude` soma outros agentes (ex.: Copilot CLI), fora do escopo do tc.
5. `tool_calls.alvo_ext` (extensão do arquivo junto do hash): **pendente de decisão**. Não implementar até decidir.
6. ~~Numeração do `turno` e `seq` compartilhada~~ **Resolvido na T22** (ver 5): turno a partir de 1, turno 0 antes do primeiro prompt; `seq` compartilhada.
7. Comando de shell na tela de sequência: hoje só aparece a classe (`teste`, `build`, `git`...). Mostrar o texto, lido do log na hora como no `tc sessao --comandos`, é decisão à parte.
8. A regra 1 (mensagem de erro em memória) do `AGENTS.md` foi atualizada na T23 (quarta exceção). A regra 2 (rede) também foi atualizada em 22/11/2026: escutar em `127.0.0.1` é permitido.
9. ~~O wrapper para scripts com a API (antiga T12) foi retirado do backlog de propósito, ou só adiado?~~ **Retirado de propósito** (dono do projeto, 22/11/2026).
10. ~~Erros em `outro` (42% de 45)~~ **Decidido** (dono do projeto, 22/11/2026): criar `codificacao_console` e `excecao_codigo`; `fatal:` do git continua em `outro`.
11. Gravar o `permissionDecision.source` em `tool_calls.aprovacao` (`automatica` | `usuario`) para o sinal "tool lenta" ignorar as tools aprovadas pelo usuário (6.5). Decisão pendente; a amostra real só tem `AskUserQuestion` aprovada pelo usuário.

---

## Referências
- CodeBurn — https://github.com/getagentseal/codeburn (docs: `docs/how-it-works.md`, `docs/providers/copilot.md`)
- ccusage — https://ccusage.com/guide/ (Cost Calculation Modes, Source Support Q&A)
- AgentTrace — https://github.com/luoyuctl/agenttrace
- Preços da Anthropic — https://platform.claude.com/docs/en/about-claude/pricing
- Prompt caching — https://platform.claude.com/docs/en/build-with-claude/prompt-caching
- Erros da API — https://platform.claude.com/docs/en/api/errors
- Copilot OpenTelemetry (VS Code) — https://code.visualstudio.com/docs/copilot/guides/monitoring-agents
