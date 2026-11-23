# token-controler

Rastreia custo e uso das suas ferramentas de AI, 100% local e gratuito.
Fase 1: Claude Code (pronta). Próximas: Copilot (arquivo de log/OTel) e o painel local no navegador (`tc web`).
O wrapper para scripts com a API saiu do backlog.

O banco guarda **só números e metadados** (modelo, tokens, nome da tool, erros, horários).
Nenhum texto de prompt, resposta ou resultado de tool é gravado. Única informação derivada de um prompt: o
rótulo `debug` (veja "Categoria e one-shot").

## Instalação

Requer Python 3.9+. A única dependência é o [rich](https://github.com/Textualize/rich), usado só nas telas visuais
(`tc painel`, `tc live` e `tc sessao`; o `pip install -e .` já o instala). Todos os outros comandos rodam só com a biblioteca padrão.

```bash
cd token-controler
pip install -e ".[dev]"   # modo desenvolvimento, com pytest
tc --help
python -m pytest          # testes
```

Sem instalar: `python -m token_controler <comando>` de dentro da pasta.

## Para agentes e desenvolvimento

- Regras do projeto: `AGENTS.md` (lidas pelo Copilot via `.github/copilot-instructions.md` e pelo Claude Code via `CLAUDE.md`).
- Design: `docs/design.md` · Backlog: `docs/tarefas.md`.
- Estrutura dos logs do Copilot (sem valores): `python scripts/inspecionar_copilot.py`.

## Uso

```bash
tc ingest                      # lê logs novos/alterados (rápido, incremental)
tc resumo                      # por projeto (padrão)
tc resumo --por dia --desde 2026-11-01
tc resumo --por modelo         # confere se algum modelo ficou sem preço
tc resumo --por sessao --hoje
tc sessoes                     # duração, tempo ativo, prompts, chamadas, custo
tc tools --projeto proj-um        # uso, taxa de erro e erros por categoria, por tool (recusas do usuário ficam fora)
tc diagnostico                 # onde as sessões ficaram caras ou lentas, com a evidência
tc sessao                      # a sessão mais recente em árvore: prompts, tools, subagentes, pausas e ⚠
tc sessao a1b2c3d4             # uma sessão específica (o começo do ID, como em `tc sessoes`)
tc sessao a1b2 --comandos      # inclui os comandos de shell, lidos agora do log (nunca gravados)
tc limpar-nomes                # apaga os nomes de arquivo gravados no banco (ver "Detalhe de uma sessão")
tc limpar-trechos              # apaga os trechos de leitura (linha inicial e quantidade) gravados no banco
tc comportamento               # categoria dos turnos, one-shot e custo por categoria
tc qualidade                   # confiança dos dados: linhas ilegíveis, sem preço, sem tokens
tc live                        # tela AGORA: sessões ativas, alertas, custo de hoje por projeto e por hora
tc painel                      # painel por período que se atualiza sozinho (teclas 1-5 mudam o período, q sai)
tc web                         # painel local no navegador (só 127.0.0.1): tela Hoje, atualizada a cada 5 s
tc ingest --tudo               # reprocessa tudo (após mudar preços)
tc ingest --modo calculate     # troca o modo de custo (ver "Modos de custo")
```

Filtros em `resumo`, `sessoes`, `tools` e `painel` (o `live` só tem `--projeto` e `--ferramenta`): `--desde`, `--ate`, `--hoje`, `--projeto`, `--ferramenta`.

### Saída JSON

`resumo`, `sessoes` e `tools` aceitam `--json`: imprimem uma lista de objetos (`[]` se não houver dados),
com os mesmos campos da tabela e números sem formatação, para usar em scripts ou no pandas:

```bash
tc resumo --por dia --json
tc sessoes --json | python -m json.tool
```

| Comando | Campos |
|---|---|
| `resumo` | a chave do agrupamento (`projeto`, `dia`, `sessao`, `modelo` ou `ferramenta`), `sessoes`, `prompts`, `chamadas`, `tools`, `erros_tool`, `erros_api`, `input`, `output`, `cache_write`, `cache_read`, `cache_taxa`, `custo_usd`, `chamadas_sem_preco` |
| `sessoes` | `sessao`, `projeto`, `inicio`, `duracao_s`, `ativo_s`, `prompts`, `chamadas`, `tools`, `erros_tool`, `custo_usd` |
| `tools` | `tool`, `chamadas`, `erros`, `taxa_erro` |

- Tokens e contagens são inteiros. `cache_taxa` e `taxa_erro` são frações de 0 a 1. `custo_usd` é em dólar.
- `null` onde a tabela mostra `-`: sem dado ou não se aplica (por exemplo `prompts` em `--por modelo`).
  `custo_usd` é `null` quando nenhuma chamada do grupo tem preço, e `chamadas_sem_preco` diz quantas ficaram sem preço
  (é o que a tabela marca com `*`). O valor nunca é chutado.
- `sessao` traz o ID completo (a tabela mostra só o início). `inicio` é ISO 8601 em UTC; `duracao_s` e `ativo_s` são segundos.
- No PowerShell 5.1, canalizar ou redirecionar a saída pode acrescentar BOM ou mudar a codificação, e o JSON deixa de
  abrir em `json.load`. Dentro do Python, prefira `subprocess`, ou leia o arquivo com `encoding="utf-8-sig"`.

### Agora (`tc live`)

Uma tela focada no **que está acontecendo agora**, para deixar aberta ao lado do trabalho. Ela relê os logs a cada
atualização e **esconde os blocos que não têm dados**:

1. **Sessões ativas** (atividade nos últimos 15 min), uma linha por sessão: projeto, **branch git** (ou "desde HH:MM" se o
   log não trouxe), última atividade ("há 2min"), custo da sessão, **custo por hora** e erros (de tool, mais "+N API" se
   houve erro de API).
2. **Alertas**, do mais grave ao menos grave, cada um com o comando para abrir a sessão (`tc sessao <id>`). São os mesmos
   problemas que o `tc sessao` mostra (falha repetida, releitura, pausa longa com cache regravado, cache baixo, re-criação de
   cache, regras e limites do `tc diagnostico`) mais **sessão cara** e **pressão de contexto** do diagnóstico.
3. **Hoje por projeto**: custo do dia (horário local) por projeto, inclusive de sessões que já pararam.
4. **Custo por hora do dia**: um mini-gráfico de 24 colunas, uma por hora local, com o pico e a hora atual.

```
token-controler · agora · 10:00:00 · hoje $2.14

Sessões ativas (últimos 15 min)
  projeto  branch / início    última atividade  custo  $/h    erros     sessão
  alfa     feature/relatorio  há 1min00s        $0.15  $2.16  3         sess-aaa
  beta     desde 09:30        há 6min00s        $0.02  -      0 +1 API  sess-bbb

Alertas
  ⚠ gama · Cache baixo: cache 25% em 18 chamadas (mínimo 80%) · $1.80  →  tc sessao sess-ccc
  ⚠ alfa · Falha repetida: Bash falhou 3 vezes seguidas · $0.02  →  tc sessao sess-aaa
```

```bash
tc live                    # atualiza a cada 10 s; r atualiza agora; q, Esc ou Ctrl+C saem
tc live --intervalo 5
tc live --projeto proj-um     # só um projeto (--ferramenta também)
tc live --simples          # texto puro, sem o rich (também é o que aparece se o rich não estiver instalado)
```

Como ler:

- **Custo por hora** = custo da sessão ÷ tempo ativo (as pausas acima de `pausa_s` não contam). Com menos de 1 min ativo
  aparece `-`, para não extrapolar de uma chamada só.
- **Alertas de evento** (uma pausa, uma falha) só aparecem se aconteceram na **última hora**
  (`live_alerta_janela_min`), para um alerta antigo não ficar grudado numa sessão longa; o estado da sessão (cache baixo,
  sessão cara) vale sempre. Só contam sessões ativas.
- **Branch**: lido do campo `gitBranch` do log **na hora, em memória**, e só mostrado: nunca vai para o banco. Sem o
  log (apagado) ou sem o campo, aparece a hora de início.
- Os limites ficam em `config.json`, na chave `diagnostico`: `live_sessao_ativa_min` (15) e `live_alerta_janela_min` (60).

A visão por período (dia, projeto, modelo, tools...) é o `tc painel`.

### Painel por período (`tc painel`)

Uma tela só, para acompanhar o dia a dia: um **resumo** no topo (custo, chamadas, sessões, cache, tokens) e uma grade
de caixas: **Por dia**, **Por projeto**, **Por atividade** (com one-shot), **Por modelo**, **Diagnóstico** (os sinais
mais importantes), **Tools mais usadas**, **Shell** (por classe de comando), **MCP** e **Qualidade dos dados**.
Ele relê os logs a cada atualização e usa 3, 2 ou 1 coluna conforme a largura do terminal.

| Tecla | O que faz |
|---|---|
| `1` `2` `3` `4` `5` | período: Hoje, 7 dias, 30 dias, Mês, Tudo |
| `r` | atualiza agora |
| `q`, `Esc` ou `Ctrl+C` | sai e devolve o terminal como estava |

```bash
tc painel                        # começa em "Hoje", atualiza a cada 15 s
tc painel --periodo 7d --intervalo 5
tc painel --projeto proj-um         # só um projeto (--ferramenta também)
tc painel --simples              # painel antigo, só uma tabela de hoje, sem o rich
```

No painel, `--desde` e `--ate` não valem: use `--periodo` ou as teclas. Se o `rich` não estiver instalado, o `tc painel`
avisa e mostra o painel simples. As cores dependem do terminal (o do VS Code e o Windows Terminal funcionam); com
a variável `NO_COLOR` definida, o painel sai sem cor.

**Como aba no VS Code:** `Ctrl+Shift+P` → **Terminal: Create Terminal in Editor Area** → `tc live` (ou `tc painel`).

### No navegador (`tc web`)

Painel local em HTML, gerado pelo Python (sem JavaScript, sem nada baixado da internet), ligado **só em `127.0.0.1`**.
Por enquanto tem a tela **Hoje**: custo (com o acumulado do dia), tokens, taxa de cache, chamadas, tool calls e erros,
tempo ativo, gasto por hora, custo por tipo de token, por projeto, por modelo, por tool e as sessões do dia com os sinais
do diagnóstico (os de severidade média e alta). As abas de período e o detalhe da sessão chegam nas próximas etapas.

```bash
tc web                 # http://127.0.0.1:8765/ ; lê os logs novos a cada 5 s e a página recarrega sozinha
tc web --porta 9000    # outra porta (ou "web_porta" no config.json); se estiver ocupada, usa a próxima livre
tc web --demo          # dados FICTÍCIOS e horário congelado, sem ingest nem recarregar: para ver a tela sem dados reais
```

- **No VS Code:** `Ctrl+Shift+P` → **Simple Browser: Show** → `http://127.0.0.1:8765/` (com o `tc web` rodando num terminal).
- **No Edge, como app (janela própria, sem abas):** `msedge --app=http://127.0.0.1:8765/`
  (no PowerShell: `Start-Process msedge "--app=http://127.0.0.1:8765/"`).

Segurança: só `GET`; o cabeçalho `Host` precisa ser `127.0.0.1:<porta>` ou `localhost:<porta>`; o servidor lê o banco
numa conexão só leitura; a página proíbe scripts e recursos externos (Content-Security-Policy); nada é registrado em log.
O banco passa a usar WAL (aparecem `tracker.db-wal` e `tracker.db-shm` ao lado dele), para o ingest gravar sem travar a
leitura. Se a tela avisar "versão antiga do tc", feche os `tc live`/`tc painel` abertos antes da atualização e rode
`tc ingest --tudo`. Testes visuais (Playwright, só desenvolvimento): `tests/visual/README.md`.

## Configuração

| O quê | Onde |
|---|---|
| Banco | `~/.token-controler/tracker.db` (mude com `TC_DB` ou `--db`) |
| Logs do Claude Code | `~/.claude/projects` (mude com `CLAUDE_CONFIG_DIR`) |
| Preços | `token_controler/precos.py` |
| Nome dos projetos | `~/.token-controler/projetos.json` (opcional) |
| Porta do `tc web` | `"web_porta"` no `config.json` ao lado do banco (padrão 8765) |

O projeto vem do nome da pasta onde a sessão rodou. Para agrupar ou renomear, crie
`~/.token-controler/projetos.json` com prefixo de caminho → nome:

```json
{
  "C:/Users/voce/repos/proj-um": "proj-um",
  "C:/Users/voce/repos/proj-dois": "proj-dois"
}
```

### Não perca histórico

O Claude Code apaga transcrições antigas (padrão de cerca de 30 dias). Rode `tc ingest`
com frequência, ou aumente o prazo em `~/.claude/settings.json`:

```json
{ "cleanupPeriodDays": 365 }
```

## Métricas

- **Chamadas**: requisições ao modelo, deduplicadas (o Claude Code grava várias linhas por resposta).
- **Prompts**: mensagens digitadas por você (exclui resultados de tool, mensagens internas e subagentes).
- **Cache w / cache r**: tokens gravados e lidos do cache. **Cache %** = leitura / input total.
- **Erros tool**: `tool_result` com `is_error`. **Erros api**: respostas marcadas como erro de API.
- **Ativo**: duração da sessão descontando pausas acima de 5 minutos.
- **Custo**: estimativa pelo preço de API. Em assinatura (Pro/Max) é o equivalente, não o valor pago.
  `*` indica chamadas sem custo: modelo fora da tabela de preços ou, no modo `display`, sem custo registrado no log.

### Diagnóstico

`tc diagnostico` (com `--sessao PREFIXO`, `--limite N`, os filtros de sempre e `--json`) aponta onde uma sessão ficou
cara ou lenta. Cada recomendação traz a **evidência em números**, a ação sugerida e, quando há conta real, o impacto
estimado em US$. A ordem é: severidade (alta, média, baixa), depois impacto em US$, depois custo da sessão.

| Sinal | Dispara quando (padrão) | Severidade |
|---|---|---|
| cache baixo | taxa de cache < 80% em sessão com mais de 10 chamadas | média; alta se < 50% |
| re-criação de cache | chamada da thread principal grava > 10 mil tokens de cache logo após pausa > 5 min | baixa; média a partir de 3. Impacto: gravar − ler, pelo preço do modelo (limite superior) |
| pausa longa | intervalo > 5 min entre eventos | baixa; média a partir de 3 |
| loop de retry | a mesma tool falha 3+ vezes seguidas | média; alta a partir de 5 |
| leitura repetida | o mesmo arquivo (por hash) lido 3+ vezes | baixa; média a partir de 5 |
| tool lenta | duração > p95 da tool **e** > 30 s (tool com 20+ medições; fora as que esperam você) | baixa; média se somar 10+ min |
| sessão cara | custo > p90 das sessões do projeto (10+ sessões) ou ≥ US$ 10 | média; alta se ≥ US$ 10. Impacto: excesso sobre o p90 |
| pressão de contexto | input total da chamada ≥ 80% da janela do modelo | média; alta ≥ 95% |

**Pressão de contexto fica desligada** até você informar a janela de cada modelo: o `tc` não presume nenhuma tabela.
Os limites acima são **propostas iniciais**, a calibrar com uso real. Para trocá-los, crie `config.json` ao lado do
banco (`~/.token-controler/config.json`):

```json
{
  "diagnostico": {
    "cache_taxa_min": 0.7,
    "custo_alto_usd": 20,
    "tools_que_esperam": ["AskUserQuestion", "ExitPlanMode", "Agent"],
    "janelas_contexto": { "claude-sonnet-5-5": 1000000, "claude-sonnet-4": 200000 }
  }
}
```

Em `janelas_contexto`, a chave é o começo do nome do modelo e o **prefixo mais longo vence** (`claude-sonnet-4-6`
ganha de `claude-sonnet-4`). Os números do exemplo são ilustrativos: confira a janela de cada modelo na página
oficial antes de usar.

`recriacao_perda_pct` (padrão 0,5) só é usada pelo `tc sessao`, para separar "cache perdido" de "conteúdo novo grande".

Chave desconhecida ou valor inválido é ignorado com um aviso. Todas as chaves e padrões estão em `LIMITES_PADRAO`,
em `token_controler/diagnostico.py`. Os sinais usam só o que já está no banco; nada é estimado pelo texto.

### Categoria e one-shot

`tc sessoes` mostra a **categoria** e o **one-shot** de cada sessão, e `tc comportamento` (com os filtros de sempre
e `--json`) soma tudo por categoria, com o custo. A conta é por **turno**: um prompt e as tools até o próximo prompt.

- **Categoria do turno**, pelas tools usadas, na ordem: planejamento (plan mode) → delegação (`Agent`) → shell sem
  edição: testes, git, build → edição = codificação → leitura, busca ou MCP (ou shell só de leitura) = exploração →
  tarefas = planejamento → sem tools = conversa → o resto = geral.
- **Debug**: se o prompt que abriu o turno fala de erro, falha ou correção (palavras em português e inglês, sem acento
  nem caixa; veja `token_controler/rotulos.py`), um turno de **codificação** ou **exploração** vira `Debug`. Testes, git,
  build, delegação, planejamento e conversa não mudam. O texto do prompt é examinado só em memória, na leitura, e
  **só o rótulo** é gravado (`eventos.rotulo`); o texto nunca é gravado, impresso nem logado. Prompts de logs já
  lidos antes dessa versão ficam sem rótulo, e `tc ingest --tudo` só rotula o que ainda existe nos logs do Claude Code.
- **Categoria da sessão**: a mais frequente entre os turnos que usaram tools. `Conversa` só se a sessão não usou nenhuma.
- **Comando de shell**: o texto é lido na ingestão só para escolher uma classe (`teste`, `build`, `git`, `leitura`,
  `outro`), e só a classe é gravada em `tool_calls.subtipo`. Comando fora das listas cai em `outro`.
- **One-shot**: de todos os turnos em que houve edição, a fração sem nenhum **retry**. Retry = o mesmo arquivo editado
  de novo depois de um comando de shell que não seja só de leitura (`rg`, `cat`, `git log`... não verificam nada).
  Sessão sem edição fica sem taxa (`-`, `null` no JSON), nunca 0%.
- **Fora, de propósito**: Feature, Refatoração e Brainstorm. Só o `debug` foi autorizado a derivar do prompt.
  Linhas gravadas antes dessa versão não têm `subtipo` nem rótulo: rode `tc ingest --tudo`.

### Tools: duração e arquivo alvo

Cada tool call guarda `duracao_ms` (do uso da tool até o resultado; `NULL` se não há resultado ou o intervalo
foge de 0 a 1 h) e `alvo_hash`, um hash do caminho do arquivo lido ou editado (só `file_path`/`notebook_path`).
**O caminho nunca é gravado.** O mesmo arquivo gera o mesmo hash em qualquer grafia (`C:\A\b.py` = `c:/a/B.py`),
o que permite contar leituras repetidas e retrabalho. O hash leva um salt aleatório do banco: impede tabelas
pré-calculadas, mas não é irreversível para quem tem o banco. Linhas gravadas antes dessa versão ficam `NULL`
até um `tc ingest --tudo`. Tools que esperam você (como `AskUserQuestion`) medem o seu tempo, não o da tool.

### Detalhe de uma sessão (`tc sessao`)

**Como abrir a árvore:** `tc sessao` (a sessão mais recente) ou `tc sessao <id>`, com o ID inteiro ou só o começo dele,
como aparece em `tc sessoes`. Funciona no terminal do VS Code; sem o `rich`, a mesma árvore sai em texto simples.
`--json` imprime os mesmos dados como JSON (sem comandos de shell).

```
Sessão a1b2c3d4 · proj-um
duração 15min20s   custo $0.87   categoria Exploração
3 prompts · 21 chamadas · 18 tools (6 com erro) · cache 74% · 1 compactação(ões)

Linha do tempo
├── Prompt 1 · 10:00:00 · Delegação · 15 tools · $0.21
│   ├── Read  analise.py · arquivo inteiro  ✓  $0.10  1.0s
│   ├── Read  analise.py · arquivo inteiro  ✓  $0.0079  1.0s  ⚠ releitura de arquivo inteiro (100% do trecho lido; já lido às 10:00:05, sem edição nem compactação)
│   ├── Read  dados.py · linhas 1–100  ✓  $0.01  30ms
│   ├── Read  dados.py · linhas 101–200  ✓  $0.01  28ms
│   ├── Read  dados.py · linhas 150–249  ✓  $0.01  27ms  ⚠ releitura de linhas 150–200 (51% do trecho lido; já lido às 10:15:10, sem edição nem compactação)
│   ├── Bash  [teste]  ✗  $0.0062  3.0s  ⚠ falha repetida 1/3
│   └── Agent  ✓  $0.0063  30s (espera)
│       └── subagente  2 tools · 3 chamadas · $0.03 · 15s
│           └── Read  analise.py  ✓  $0.03  1.0s
├── ⏸ pausa de 9min20s ⚠ cache expirado, 48,000 tokens regravados · $0.23 a mais que ler do cache
└── Prompt 2 · 10:12:00 · ...
```

Cada prompt é um galho (só o horário e a categoria: **o texto do prompt nunca aparece**), com as tools chamadas (nome da
tool, arquivo, ✓ ok / ✗ erro, custo e tempo), os subagentes aninhados na chamada `Agent`/`Task` que os disparou e a
pausa entre prompts. No topo: projeto, duração, custo e categoria. No fim, os **⚠ problemas**, cada um com o motivo e o
custo envolvido, e sugestões curtas para a sessão custar menos. Os problemas reaproveitam as regras e os limites do
`tc diagnostico` (os mesmos de `config.json`, chave `diagnostico`):

| ⚠ | Quando | Custo envolvido |
|---|---|---|
| Falha repetida | a **mesma tool no mesmo alvo** falha `loop_min` (3) vezes seguidas; 5+ é gravidade alta. (O `tc diagnostico` conta só por tool, sem o alvo) | custo das chamadas que pediram as tentativas |
| Releitura desnecessária | um trecho do arquivo (por hash) lido de novo, na mesma thread, **sem edição nele nem compactação** entre as leituras, repetindo **25% ou mais** do trecho que acabou de ser lido (`releitura_min_sobreposicao`). Ler as linhas 1–100 e depois 101–200 não é releitura; 150–249 depois de 101–200 repete 51 de 100 linhas (51%) e é. Leituras anteriores pequenas somam (união das linhas); 3 linhas repetidas num trecho de 80 (3,75%) não contam. Sem `limit`, o `Read` lê no máximo 2000 linhas, e é essa a base. Releitura depois de um Edit/Write com sucesso ou depois de `/compact` é normal e não conta | custo das chamadas que pediram as releituras |
| Pausa longa | mais de `pausa_s` (5 min) sem atividade **e** a chamada seguinte regravou mais de 10 mil tokens de cache (o cache expirou). Se o cache foi só lido, a pausa aparece apagada, sem ⚠, com a nota "cache não regravado" (comum com cache de 1 h) | gravar − ler do cache, pelo preço do modelo (limite superior) |
| Cache baixo | taxa de cache abaixo do mínimo (80%) em sessão com mais de 10 chamadas | custo das gravações de cache da sessão |
| Re-criação de cache | chamada que grava mais de 10 mil tokens de cache **e** quase não leu o cache (leitura abaixo de `recriacao_perda_pct`, 50%, do contexto anterior), fora de pausa e de compactação | gravar − ler, pelo preço do modelo |

Como ler os números:

- **Custo de uma tool** = custo da chamada do modelo que a pediu, dividido igualmente entre as tools pedidas juntas.
  O que a tool devolveu entra no custo da chamada seguinte. Nada é estimado pelo texto: o custo do prompt é a soma das
  chamadas dele (inclusive as dos subagentes), e a soma dos prompts fecha com o custo da sessão.
- **Trecho lido (`offset`/`limit`)**: a tool `Read` pode ler só um pedaço do arquivo. Por isso o banco guarda, só nos `Read`,
  os dois números do trecho (`leitura_ini`, a linha inicial, e `leitura_qtd`, quantas linhas; sem `limit`, vai até o fim),
  e a releitura só vale se as faixas de linhas se sobrepõem. Antes disso, as releituras de um arquivo grande lido por
  partes apareciam como falsos alertas (4 numa sessão real, trechos 186, 340, 660 e 805 do mesmo arquivo). Sessões
  ingeridas antes dessa coluna não têm o trecho: aparece "**possível** releitura" (gravidade baixa) e o `tc ingest --tudo`
  resolve enquanto o log existir. Leituras com `pages` (PDF) ficam sem trecho. O `tc` ainda **não vê mudanças feitas por
  comandos de shell**. Por isso **imagens ficam fora da regra** (`.jpg`, `.png`, `.gif`, `.webp`, `.bmp`, `.tif`, `.heic`,
  `.avif`, `.ico`): costumam ser recortadas ou ampliadas por um script e lidas de novo, já com outro conteúdo. A lista é
  a chave `releitura_ignora_extensoes` (em `diagnostico`, no `config.json`; `[]` desliga a exceção) e depende do nome do
  arquivo gravado: com `gravar_nome_arquivo` desligado, ou em sessões sem nome, a extensão não é conhecida e o arquivo
  é checado. Trate a releitura como um indício.
- Sessões ingeridas antes do nome do arquivo ser gravado mostram o **hash curto** (`#a1b2c3d4`) no lugar do nome;
  `tc ingest --tudo` preenche, mas só enquanto o log original existir. Sessão inexistente ou sem tools é dita com clareza.

**Nome do arquivo (teste, decisão do dono em 22/11/2026).** A coluna `tool_calls.alvo_nome` guarda só o **nome** do arquivo
(`analise.py`), sem pasta: separadores `/` e `\` são removidos e o tamanho é limitado a 80 caracteres. Caminho completo,
comando de shell, argumentos e conteúdo continuam proibidos; o `alvo_hash` do caminho completo fica, e é ele que separa
`src\analise.py` de `tests\analise.py`. Para desligar, ponha em `~/.token-controler/config.json`:

```json
{ "gravar_nome_arquivo": false }
```

e rode `tc limpar-nomes` para apagar o que já foi gravado (ele faz `VACUUM`, então os nomes somem do arquivo do banco).
O comando avisa se a gravação ainda estiver ligada: sem desligar a chave, o próximo `tc ingest` volta a gravar.

**Trecho lido (teste, terceira exceção da regra 1, 22/11/2026).** Só nos `Read`, `tool_calls.leitura_ini` e `leitura_qtd` guardam
`offset` e `limit` como números inteiros (nunca outro argumento, texto ou caminho). Para desligar:
`{ "gravar_trecho_leitura": false }` no mesmo `config.json`, e `tc limpar-trechos` apaga o que já foi gravado.
Para preencher sessões antigas, `tc ingest --tudo` (só enquanto o log existir).

**Comandos de shell** não aparecem por padrão. Com `--comandos`, são lidos **na hora, do log original**, em memória, e
nunca gravados (com o log já apagado, o nó mostra "comando indisponível"). `--comandos` não vale com `--json`.

### Erros por categoria (`tc tools`)

Quando uma tool falha, o `tc` lê a mensagem de erro **só em memória**, classifica por regras fixas (`token_controler/regras_erros.py`) e
grava **apenas** a categoria (`tool_calls.erro_categoria`) e o código de saída do shell (`exit_code`). A mensagem nunca é gravada.
As categorias: `recusado_usuario`, `bloqueado_permissao`, `mcp_auth`, `modulo_ausente`, `comando_ausente`, `sintaxe_shell`,
`codificacao_console`, `edicao_invalida`, `arquivo_ausente`, `permissao`, `timeout`, `teste_falhou`, `rede`, `excecao_codigo` (Traceback do código rodado) e `outro` (design §6.4).

- **`recusado_usuario`** (você negou a tool na tela de permissão) **não conta como erro**: fica fora da taxa de `tc tools`,
  `tc resumo`, `tc sessoes`, `tc live` e dos loops; `tc tools` mostra a coluna `recusadas` à parte.
- Erros lidos antes desta versão aparecem como `sem_categoria`: rode `tc ingest --tudo` (só reclassifica o que o log ainda tem).
- O `tc ingest` também grava, por tool e chamada, o `turno`, a ordem `seq` na sessão, a `chamada_id` que pediu a tool, o
  `subagente_id` e o `servidor_mcp` (para a futura tela de sequência do `tc web`).

### Qualidade dos dados

`tc qualidade` (também com `--json` e `--ferramenta`) diz quanto dá para confiar nos números, por ferramenta:

- **linhas ilegíveis**: linhas dos logs que não são JSON válido e foram descartadas (contadas por arquivo, sem dobrar ao reprocessar);
- **sem tokens**: chamadas em que todos os contadores de tokens são zero. O `tc` não estima tokens: só aponta a lacuna;
- **sem preço**: chamadas de modelos fora de `precos.py` (confere contra a tabela atual, em qualquer modo de custo);
- **registrado / calculado / sem custo**: de onde veio o custo de cada chamada;
- **erros de tool em `outro`**: quantos erros não casaram com nenhuma categoria (quanto maior, mais regras faltam em
  `regras_erros.py`) e quantos ainda estão sem categoria (rode `tc ingest --tudo`);
- **confiança**: `baixa` (falta preço ou tokens, ou há linhas ilegíveis), `media` (só falta a contagem de linhas
  ilegíveis) ou `alta`. Os motivos aparecem abaixo da tabela. Os limites são uma proposta inicial.

Chamadas de erro de API ficam fora de "sem tokens" e "sem preço". O nível de detalhe por sessão ainda não existe
(entra com a T09) e aparece como `T09` na tabela e `null` no JSON. Arquivos lidos antes dessa versão não têm a contagem
de linhas ilegíveis (`-`); rode `tc ingest --tudo` uma vez para preenchê-la.

### Modos de custo

Cada chamada guarda de onde veio o custo (`origem_custo`): `registrado` (o log trouxe o `costUSD`),
`calculado` (tokens × `precos.py`) ou `sem_preco` (sem custo; o valor fica `NULL`, nunca chutado).
Chamada de erro de API não tem custo nem origem. O modo escolhe qual vale:

| Modo | O que faz |
|---|---|
| `auto` (padrão) | usa o custo registrado quando o log traz (inclusive zero); senão calcula |
| `calculate` | ignora o registrado e sempre calcula pelos tokens |
| `display` | só usa o registrado; chamada sem custo registrado fica `sem_preco` |

`tc ingest --modo calculate` grava o modo no banco e recalcula **todas** as chamadas só com o que já está
nele, sem reler os logs (que o Claude Code apaga). Sem `--modo`, o `ingest` mantém o modo atual.
O rodapé de `tc resumo` mostra o modo ativo e quantas chamadas vieram de cada origem.

Bancos criados antes dessa versão são migrados sozinhos na primeira execução. As chamadas antigas ficam
`calculadas`, como eram; para capturar o `costUSD` dos logs que ainda existirem, rode `tc ingest --tudo`.

## Schema

`chamadas`, `tool_calls` (com `alvo_hash` e, em teste, `alvo_nome`, `leitura_ini` e `leitura_qtd`), `eventos` (`prompt`, `atividade` e `compactacao`, só com o horário), `arquivos` e `config` (modo de custo). As quatro primeiras têm a coluna `ferramenta`, para que
novas fontes (Copilot, API) entrem no mesmo banco sem mudar os relatórios.
Dá para consultar direto: `sqlite3 ~/.token-controler/tracker.db`.

## Limitações conhecidas

- O formato dos logs do Claude Code não é documentado oficialmente e pode mudar entre versões.
  A Fase 1 foi testada com logs sintéticos; confira os números com `/cost` ou com o ccusage na primeira vez.
- O campo `costUSD` é lido como o ccusage documenta, mas nos logs reais usados até agora (Claude Code atual)
  ele não aparece: lá tudo é `calculado`. O caminho `registrado` foi testado só com fixture sintética.
- Erros de API que o SDK resolve sozinho com retry não aparecem nos logs (para isso, use OpenTelemetry).
