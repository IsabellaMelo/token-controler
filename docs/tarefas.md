# Backlog

Uma tarefa por vez, na ordem. Cada uma tem critério de aceite; só marque `[x]` com os testes passando.
Detalhes de cada item em `docs/design.md`.

## Referências

Algumas tarefas têm uma linha **Referência**, que aponta um projeto open source (licença MIT) em
`../referencias/` onde aquela parte já foi resolvida. Quando a tarefa tiver referência:

1. Leia a referência **antes** de planejar (somente leitura; nunca edite nada em `../referencias/`).
2. No plano, liste o que vai **aproveitar** e o que vai **descartar** por conflitar com o `AGENTS.md`
   (estimar tokens pelo texto, buscar preços na rede, caches próprios, gravar texto ou caminhos).
3. **Reimplemente** em Python no estilo de `fontes/claude_code.py`. Não traduza linha a linha.
4. Se um trecho ficar uma adaptação próxima do original, registre o crédito em `NOTICE.md`.
5. Se `../referencias/<projeto>` não existir, avise e siga sem ela.

Para clonar (na pasta acima do repo):

```bash
mkdir referencias && cd referencias
git clone --depth 1 https://github.com/ccusage/ccusage.git
git clone --depth 1 https://github.com/getagentseal/codeburn.git
git clone --depth 1 https://github.com/luoyuctl/agenttrace.git
```

No Claude Code, abra com `claude --add-dir ../referencias`. No VS Code, use *File → Add Folder to Workspace*.

Os caminhos citados abaixo são pontos de partida; se não existirem, procure no repositório.

## Fase 1 — Claude Code ✅
- [x] Schema SQLite (`chamadas`, `tool_calls`, `eventos`, `arquivos`)
- [x] Adaptador Claude Code com deduplicação e ingestão incremental
- [x] CLI: `ingest`, `resumo`, `sessoes`, `tools`, `live`
- [x] Testes com fixtures sintéticas e CI

## Fase 1.5 — Validação com dados reais
- [x] **T01 · Conferir o Claude Code com dados reais.** Rodar `tc ingest` e `tc sessoes` e comparar uma sessão com o `/cost` do Claude Code.
  Aceite: diferença de custo < 5% ou causa explicada e corrigida, com teste novo cobrindo o caso.
  Concluída em 21/11/2026 sem usar o `/cost` (ele só mostra a sessão aberta): validação somente leitura de 7 sessões reais contra o ccusage (`ccusage claude session --json --offline`) e contra `scripts/verificar_totais.py`. Tokens e custo bateram em todas (total US$ 64,10, diferença 0). Nenhuma correção foi necessária.
  Referência: **ccusage**, o leitor de logs do Claude Code (campos lidos e deduplicação). Se o Node estiver instalado, compare também os totais por sessão com `npx ccusage session`: primeiro os tokens, depois o custo (os preços podem diferir).
- [x] **T02 · Saída `--json`** em `resumo`, `sessoes` e `tools`.
  Aceite: `tc resumo --json` imprime uma lista de objetos com os mesmos campos da tabela, números sem formatação.
  Referência: **ccusage**, guia *JSON Output* (formato e nomes de campos).

- [x] **T21 · Confirmar nos logs reais os campos da sequência.** Antes de gravar qualquer coisa nova, conferir nos logs reais do Claude Code:
  `tool_use_id` (liga `tool_use` e `tool_result`), `parentUuid` (ordem e encadeamento das mensagens), a **ligação com o subagente** (como a
  chamada `Task`/`Agent` aponta para o arquivo ou o ID do subagente) e o **formato do `tool_result` de erro** (campos, `is_error`, onde
  aparece o código de saída, como a recusa na tela de permissão e o erro de MCP se apresentam).
  Aceite: `docs/formatos/claude_code.md` com a estrutura (campos e tipos, **sem valores**) e a decisão sobre `subagente_id`, `turno` e `seq`
  (design §5 e §11). Usar um script que imprime só estrutura e, para os erros, só a **contagem por categoria candidata**, nunca o texto
  (se faltar uma amostra, pedir a estrutura em vez de inventar, como no `AGENTS.md`). Sem isso, T22 e T23 não começam.
  Feito (22/11/2026): `scripts/inspecionar_claude_code.py` e `docs/formatos/claude_code.md`. Confirmou `tool_use_id`, `parentUuid`,
  `promptId` (turno) e o vínculo `agentId` (3 de 3). Divergências do design original: a tool se chama `Agent`; a recusa tem campo próprio
  (`toolDenialKind`), com bloqueios por regra que não são recusa; exit code só existe no texto do erro; `duracao_ms` inclui a espera pela
  aprovação; 21 de 47 erros (45%) caíam em `outro` com as 11 categorias. Decididas com o dono do projeto: categorias
  `bloqueado_permissao` e `edicao_invalida` (13 no total). Sem nenhuma chamada MCP na amostra.

## Fase 2 — Copilot no VS Code
- [ ] **T03 · Levantar o formato.** Rodar `scripts/inspecionar_copilot.py` no notebook do trabalho e salvar a estrutura (sem valores) em `docs/formatos/copilot.md`.
  Aceite: documento com os campos de sessão, requisição, modelo, tokens, timestamps e tools; e se `agent-traces.db` existe.
  Referência: **codeburn**, `docs/providers/copilot.md`, para comparar com o que foi encontrado. Fonte da verdade é a estrutura real.
- [ ] **T04 · Fixture sintética do Copilot** em `tests/fixtures/copilot/`, escrita à mão seguindo `docs/formatos/copilot.md`, sem nenhum conteúdo real.
  Referência: **codeburn**, fixtures de teste do provider Copilot (se houver), para cobrir casos de borda.
- [ ] **T05 · Adaptador `fontes/copilot.py` (chatSessions).** `ferramenta='copilot'`, tokens de `promptTokens`/`outputTokens`, requisições sem tokens contadas como "sem dados" (nunca estimadas).
  Aceite: testes de contagem, deduplicação, custo, incremental e "não grava texto", como em `test_claude_code.py`.
  Referência: **codeburn**, parser do Copilot em `src/providers/` e `docs/providers/copilot.md`. Descartar: estimativa de tokens pelo tamanho do texto.
- [ ] **T06 · Adaptador `agent-traces.db`** (se existir): lido em modo somente leitura; quando presente, tem prioridade sobre os chatSessions da mesma sessão, sem contar em dobro.
  Referência: **codeburn**, leitura do armazenamento OTel do Copilot em `src/providers/`.
- [ ] **T07 · `tc ingest` lê todas as fontes** e informa o resultado por ferramenta.
- [x] **T08 · Coluna `origem_custo`** (`registrado`, `calculado`, `sem_preco`) e modos de custo `--modo auto|calculate|display`.
  Referência: **ccusage**, guia *Cost Calculation Modes* e a implementação dos modos. Descartar: busca de preços na rede.
- [ ] **T09 · Nível de detalhe por sessão** (`completo`, `agregado`, `limitado`), exibido em `tc sessoes` e em `tc qualidade` (campo `nivel_detalhe`, hoje `None`).
  Referência: **agenttrace**, níveis `Detailed` / `Aggregate` / `Limited` (README e código).
- [ ] **T10 · Preços de modelos não Anthropic** usados no Copilot (GPT, Gemini), adicionados à mão em `precos.py` com a fonte e a data no comentário; Copilot exibido também em créditos (1 crédito = US$ 0,01).
  Referência: **codeburn**, preços embutidos de reserva (fallback), só como ponto de partida; confirmar nas páginas oficiais de preço.
- [x] **T11 · `tc qualidade`**: linhas ilegíveis, modelos sem preço, requisições sem tokens e nível de detalhe, por ferramenta.
  Pendente dentro da T11: o **nível de detalhe** aparece como "não disponível" (`T09` na tabela, `null` no JSON) porque
  depende da T09. Ao fazer a T09, ligar o `nivel_detalhe` em `token_controler/qualidade.py` (campo hoje fixo em `None`).
  Referência: **agenttrace**, relatório de confiança dos dados (parse skips, fontes e modelos desconhecidos, preços de reserva).

> A Fase 2 tem duas frentes independentes: o **Copilot** (T03 a T10) e os **campos novos do Claude Code** (T22 e T23), que o `tc web` e o
> diagnóstico usam. Elas não dependem uma da outra.

- [x] **T22 · Dados da sequência (design §5).** O `ingest` do Claude Code passa a gravar `turno` e `seq` (em `chamadas` e `tool_calls`),
  `tool_calls.chamada_id` (via `tool_use_id`), `tool_calls.subagente_id` (liga a `Task` às chamadas e tools do subagente) e
  `tool_calls.servidor_mcp` (`mcp__google-drive__search_files` → `google-drive`). `duracao_ms` já existe (T13). Migração segura em `db.py`
  (colunas novas e índices por `(sessao_id, seq)`); `tc ingest --tudo` repovoa.
  **Não implementar `alvo_ext`** (pendente de decisão, design §11).
  Aceite: fixture sintética com 2 turnos, tools paralelas na mesma chamada, um subagente e uma tool MCP; testes de cada coluna; banco sem
  texto novo; `pytest` passando. Opcional depois: trocar a atribuição de custo por horário do `detalhe.py` por `chamada_id`.
  Depende da T21.
  Feito: `chamadas` ganha `turno`, `seq`, `subagente_id`; `tool_calls` ganha `turno`, `seq`, `chamada_id`, `subagente_id`, `servidor_mcp`
  (mais `exit_code` e `erro_categoria` da T23). `turno` = nº do prompt (0 antes do primeiro; subagente herda o da chamada `Agent`, NULL se
  a ligação não existir). `seq` = 1..n por sessão, por horário, com a chamada antes das tools que ela pediu e o subagente no meio da
  thread principal (`claude_code.ligar_sessoes`, que roda no fim de cada `ingest`). `subagente_id` = `agentId`, pelo
  `toolUseResult` e, de reserva, pelo `toolUseId` do `.meta.json`. Sem `alvo_ext`. Nos dados reais: 3 de 3 `Agent` ligados, 7 de 7
  tools de subagente com turno, 0 tools sem `chamada_id`, `seq` contínuo em todas as sessões. Bancos antigos ficam com NULL até
  `tc ingest --tudo`. `tool` continua com o nome completo (`mcp__…`). Testes: `tests/test_erros_ingest.py`.
- [x] **T23 · Categorias de erro (design §6.4).** `token_controler/regras_erros.py` com as **13** categorias (as 11 do design mais
  `bloqueado_permissao` e `edicao_invalida`, T21), testadas **na ordem da tabela**
  (`recusado_usuario` e `mcp_auth` primeiro), em português e inglês, Windows e Unix. O `ingest` lê a mensagem do `tool_result` **só em
  memória** e grava apenas `tool_calls.erro_categoria` e `tool_calls.exit_code`. `recusado_usuario` fica fora da taxa de erro. `tc qualidade`
  conta os erros que caíram em `outro`. `tc ingest --tudo` reclassifica.
  **Antes de codar:** registrar no `AGENTS.md`, regra 1, a nova exceção (mensagem de erro lida em memória; só categoria e código de saída
  gravados), com a aprovação do dono do projeto.
  Aceite: um teste por categoria com mensagens sintéticas (nenhuma real), um teste da ordem (uma mensagem que casaria com duas categorias),
  teste de que a mensagem não chega ao banco, `tc qualidade` com a contagem de `outro`, `pytest` passando. Depende da T21.
  Feito: exceção registrada no `AGENTS.md` (regra 1, quarta exceção) antes do código. `classificar(texto, tool, servidor_mcp, negacao)` e
  `codigo_de_saida` são funções puras; `recusado_usuario` e `bloqueado_permissao` vêm de `toolDenialKind` (sem ler texto; em log antigo,
  sem o campo, a recusa cai no texto). `mcp_auth` só vale em tool MCP e `edicao_invalida` só em `Edit`/`Write`. `exit_code` só em falha de
  shell. A recusa fica fora da taxa de erro de `tc tools`, `tc resumo`, `tc sessoes`, painel, `tc live`, `tc sessao` e da corrida de falhas
  do diagnóstico (`db.ERRO_REAL`). `tc tools` mostra a coluna `recusadas` e o bloco "Erros por categoria"; `tc qualidade` conta `outro`.
  Testes: `test_regras_erros.py` (95) e `test_erros_ingest.py` (31, com a prova de que nenhuma mensagem chega ao banco nem à saída).
  **Medido nos logs reais** (45 erros, sem as 2 recusas): `outro` ficou com 19 (42%): 8 têm `Traceback` de Python genérico, 5 são erro de
  codificação do console do Windows (`charmap`/`UnicodeEncodeError`), 3 são `fatal:` do git (exit 128), 1 foi morto (exit 143). Candidatas
  a novas categorias, fora do escopo aprovado.
  **Rodada seguinte (22/11/2026, aprovada pelo dono do projeto):** `codificacao_console` e `excecao_codigo` (15 categorias). `excecao_codigo`
  conta para o loop de retry mas **não** para o "erro recorrente" (`regras_erros.conta_para_erro_recorrente`, usada na T29) e não grava o
  nome da exceção. `fatal:` do git continua em `outro`. `Task` e `Agent` são aceitos como tool de subagente (testado com os dois). Não
  existe `espera_aprovacao_ms`: o log não registra quando a aprovação foi pedida (design 6.5).

## Fase 2.5 — `tc web` (painel no navegador)
Design em §8.1. Python + HTML + CSS gerados no servidor, **sem JavaScript e sem bibliotecas**; só `127.0.0.1`. Uma tarefa por tela, nesta ordem.

- [ ] **T24 · Servidor base e a tela Hoje.** `tc web [--porta N]` com `http.server.ThreadingHTTPServer` ligado só em `127.0.0.1` (porta ocupada:
  tenta a próxima). Conexões do servidor com o SQLite em `mode=ro`; o banco passa a usar **WAL** (`.gitignore` para `*.db-wal` e `*.db-shm`;
  validar no Windows o `mode=ro` com WAL). Thread de `ingest` incremental no mesmo intervalo da página (`<meta http-equiv="refresh" content="5">`).
  Tela **Hoje**: 6 KPIs (custo com sparkline, tokens, taxa de cache, chamadas, tool calls e erros, tempo ativo); gasto por hora; custo por
  tipo de token (rosca com `conic-gradient`); por projeto, por modelo e por tool (barras em `div`); tabela de sessões com sinais.
  Gráficos: barras em `div`, rosca em `conic-gradient`, linhas em SVG `<polyline>`, tooltip em `title`; fontes do sistema.
  **Antes de codar:** a regra 2 do `AGENTS.md` ("nunca fazer chamada de rede") precisa dizer que escutar em `127.0.0.1` é permitido
  (sem conexão de saída). *Feito em 22/11/2026: a regra 2 já tem essa exceção.*
  Aceite: testes sem rede externa (servidor em porta livre, `urllib` só para `127.0.0.1`), `html.escape` em todo texto do banco, só `GET`,
  checagem do `Host`, banco aberto só leitura (uma escrita pela conexão do servidor falha), porta ocupada → próxima, sem `<script>`
  no HTML, `pytest` passando e README atualizado.
  *Implementado em 22/11/2026, aguardando a aprovação visual:* `token_controler/web/` (servidor, telas, modelos), `consultas.dia`
  e `consultas.custo_por_tipo`, `tc web --demo` (`token_controler/demo.py`, `scripts/gerar_demo.py`), testes em
  `tests/test_web.py` e revisão visual com Playwright em `tests/visual/` (job `visual` no CI). Os prints de referência só
  entram depois da aprovação. Decisões no design §8.1.
- [ ] **T25 · Abas de período e filtros.** Estado na URL: `/?periodo=7d&projeto=x` (períodos como no `tc painel`: hoje, 7d, 30d, mês, tudo).
  Aceite: a URL reproduz a tela; filtro inválido não quebra; links entre abas preservam o projeto.
- [ ] **T26 · Detalhe da sessão** (`/sessao/<id>`, aceita o prefixo do ID). KPIs da sessão; **chamadas por minuto** (linha em SVG) com marcas
  para pausas, re-criações de cache, loops e prompts; **diagnóstico** com as recomendações (as mesmas regras do `tc diagnostico` e do
  `tc sessao`); tokens e tools. Aceite: sessão inexistente ou ambígua com mensagem clara; fixture com um caso de cada marca.
- [ ] **T27 · Sequência da sessão: turnos e passos** (`/sessao/<id>/sequencia?turno=3`). Turnos à esquerda; no centro, os passos do turno em
  ordem (prompt, modelo, leitura, escrita, shell, MCP, subagente), cada um com horário, detalhe, duração, custo e uma **barra de início e
  duração dentro do turno**. Usa `turno`, `seq`, `chamada_id`, `subagente_id` e `servidor_mcp` (T22). O detalhe é só o que já é permitido
  gravar; prompts e comandos de shell **não** aparecem em texto. Depende da T22 e da T26.
  Aceite: fixture com subagente, tools paralelas e MCP; custo do passo bate com o da chamada; sessão sem as colunas novas mostra "sem dados".
- [ ] **T28 · Sequência: loops e filtros.** Loops de retry (3 ou mais falhas seguidas da mesma tool) agrupados num bloco só; filtros por tipo
  de passo e "só erros" (`?turno=3&tipo=erros`); passos `recusado_usuario` não contam como falha (T23).
  Aceite: filtros combináveis pela URL; loop agrupado mostra quantas tentativas e o custo somado. Depende da T27 e da T23.

## Fase 3 — Comportamento e diagnóstico
- [x] **T13 · `tool_calls.duracao_ms` e `alvo_hash`** (hash do caminho, nunca o caminho).
  Feito: duração = resultado − uso, aceita entre 0 e 1 h (senão `NULL`); hash HMAC com salt por banco, só para `file_path`/`notebook_path`.
  Bancos antigos ficam com `NULL` até `tc ingest --tudo`. Atenção na T16: tools que esperam a pessoa (`AskUserQuestion`) medem o tempo humano, não da tool.
  Referência: **agenttrace**, como mede duração de tools e identifica alvos sem guardar o conteúdo.
- [x] **T14 · Categoria da sessão** por regra determinística (ver design §6.2).
  Feito: 10 categorias (codificação, debug, exploração, testes, git, build, delegação, planejamento, conversa, geral), por turno e depois por sessão; `tc sessoes` e `tc comportamento`.
  Debug (adicionado depois, com aprovação): um rótulo `debug` derivado das palavras do prompt na leitura; só o rótulo vai ao banco (exceção registrada na regra 1 do `AGENTS.md`). Feature, Refatoração e Brainstorm seguem fora.
  Referência: **codeburn**, `docs/how-it-works.md` (tabela das 13 categorias) e o código do classificador.
- [x] **T15 · Taxa de one-shot** (ver design §6.2).
  Feito: retry = mesmo `alvo_hash` editado de novo com shell que não seja só de leitura no meio; one-shot = turnos de edição sem retry. Em `tc sessoes` e `tc comportamento`.
  Diferença para o CodeBurn: cada tool vale um passo (o banco não guarda o agrupamento de chamadas paralelas).
  Referência: **codeburn**, `docs/how-it-works.md` (definição de retry) e o código da taxa de one-shot.
- [x] **T16 · `tc diagnostico`**: sinais do design §6.3 com limites em `config.json` e recomendações ordenadas por impacto, sempre com a evidência (números) ao lado.
  Feito: os 8 sinais do design, limites em `config.json` (chave `diagnostico`), ordem por severidade, impacto em US$ e custo da sessão.
  Limites do AgentTrace anotados no plano: loop 3 (alta) e 5 (crítica) → adotados só para falhas seguidas; falhas ≥3 e ≥20%, pausas >120 s, tools raras ≤2 e parâmetros >10 kB → não adotados; tool lenta p95 >30 s e custo ≥US$ 10 → adotados como piso/limite alto; janelas de contexto e risco por tamanho do texto → descartados (regra 4), a janela vem do `config.json`.
  Pressão de contexto fica desligada sem `janelas_contexto`. Todos os limites são propostas iniciais para a T17.
  Referência: **agenttrace**, detecção de retry loops, pausas longas, sessões travadas, leituras repetidas, pressão de contexto, tools lentas e as recomendações. Anotar no plano os limites que ele usa.

- [x] **T19 · `tc sessao <id>`**: detalhe de uma sessão em árvore (`rich.Tree`) para entender o que aconteceu dentro dela.
  Etapa 1 (privacidade, decisão do dono em 22/11/2026, em modo de teste): regra 1 do `AGENTS.md` passa a permitir o NOME do arquivo
  tocado por uma tool, sem a pasta, em `tool_calls.alvo_nome` (continua proibido caminho completo, comando de shell, argumentos e conteúdo;
  o `alvo_hash` do caminho completo fica). Chave `gravar_nome_arquivo` (padrão `true`) e `tc limpar-nomes` para voltar atrás.
  Etapa 2: árvore cronológica (prompt → tools → subagentes aninhados, pausas entre prompts) com ⚠ para falha repetida, releitura
  desnecessária (sem edição nem compactação entre as leituras), pausa longa e cache baixo/re-criação, cada um com motivo e custo,
  mais sugestões no fim. Reaproveita as regras e os limites do `tc diagnostico`. Comandos de shell só com `--comandos`, lidos do log
  na hora e nunca gravados.
  Aceite: testes com fixture sintética (incluindo releitura legítima e desnecessária), valor gravado sem separador de pasta, banco sem
  caminho completo, `pytest` passando e README atualizado.
  Referência: **agenttrace** (leituras repetidas, retry loops, pausas), já usada na T16; os limites são os mesmos.
  Feito: `detalhe.py` (dados e árvore neutra, só biblioteca padrão) e `painel.renderizar_sessao` (rich, o único módulo que o importa).
  `diagnostico.py` ganhou funções reutilizáveis (`corridas_de_falhas`, `pausas_longas`, `recriacoes_de_cache`, `impacto_recriacao`)
  sem mudar o comportamento dos sinais. O banco passou a ter o evento `compactacao` (só o horário, de `system/compact_boundary`) e o
  resumo de compactação deixou de contar como prompt. Re-criação fora de pausa exige a leitura de cache despencar
  (`recriacao_perda_pct`); sem isso, qualquer leitura pesada (cache_write > 10k) parecia re-criação (46 falsos positivos em 9 sessões reais).
  Terceira exceção da regra 1 (22/11/2026, em teste): `Read` grava `leitura_ini`/`leitura_qtd` (offset e limit, só inteiros;
  `gravar_trecho_leitura`, `tc limpar-trechos`). A releitura só vale com linhas sobrepostas; sem o trecho gravado (sessão antiga),
  é "possível". Com logs reais, as 4 releituras falsas (trechos diferentes de um arquivo grande) sumiram.
  Limite que sobra: alteração por comando de shell não é vista; por isso imagens (`releitura_ignora_extensoes`) ficam fora da regra.
  Releitura só conta com `releitura_min_sobreposicao` (25%) ou mais do trecho lido agora (3 linhas repetidas de 80 eram ruído);
  base de um `Read` sem `limit` = 2000 linhas, o padrão do Read.

- [x] **T20 · Separar o `tc live` do `tc painel`.** O painel (abas de período, grade de caixas) passa a se chamar `tc painel`, com as mesmas
  opções e o mesmo comportamento. O `tc live` vira a tela AGORA: uma linha por sessão ativa (últimos 15 min) com projeto, branch git (se o
  log tiver; senão a hora de início), última atividade, custo, custo por hora e erros; alertas, cada um com o `tc sessao <id>`; custo de
  hoje por projeto; mini-gráfico de custo por hora do dia. Blocos sem dados não aparecem. Reaproveita as regras do `tc diagnostico`.
  Aceite: testes com fixture de 3 sessões simultâneas (mais uma parada, para provar a janela de 15 min), `pytest` passando e README atualizado.
  Feito: `agora.py` (dados e texto simples, só biblioteca padrão), `painel.renderizar_agora`/`executar_agora` (rich), `cli.cmd_live` novo e
  `cmd_painel` (o antigo). Alertas = `detalhe.analisar` (as regras do `tc diagnostico`, o mesmo que o `tc sessao` mostra) + `sessao_cara` e
  `pressao_contexto`; só eventos da última hora (`live_alerta_janela_min`). Custo/hora = custo ÷ tempo ativo (`-` com menos de 1 min).
  O branch vem do `gitBranch` do log, lido na hora e **nunca gravado** (nenhuma exceção nova na regra 1). Na regra 3 do `AGENTS.md`
  só os nomes dos comandos foram atualizados; o `rich` continua só no `painel.py`.

- [ ] **T29 · Sinais por categoria de erro** (design §6.3 e §6.4). **Loop de retry:** se todas as falhas da corrida têm a mesma
  `erro_categoria`, o sinal usa a recomendação da categoria. **Erro recorrente (novo):** a mesma categoria em 3 ou mais sessões do projeto em
  7 dias, com limites em `config.json` (chave `diagnostico`) como os demais. `recusado_usuario` fica fora dos dois.
  Aceite: testes com fixtures de várias sessões; o `tc diagnostico`, o `tc sessao` e o `tc live` mostram as recomendações novas; README
  atualizado. Depende da T23.

## Fase 4 — Calibração
- [ ] **T17 · Ajustar limites** do diagnóstico depois de 2 a 3 semanas de uso real.
  Referência: **agenttrace**, baselines locais e checagem de regressão.
