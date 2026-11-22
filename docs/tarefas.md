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

## Fase 3 — Scripts com a API
- [ ] **T12 · Wrapper** `token_controler.api.registrar(resposta, ...)` para scripts que usam o SDK da Anthropic: grava `usage`, latência, modelo, `request_id`, erros e retries com `ferramenta='api'`. Sem importar o SDK no pacote.

## Fase 4 — Comportamento e diagnóstico
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

## Fase 5 — Calibração
- [ ] **T17 · Ajustar limites** do diagnóstico depois de 2 a 3 semanas de uso real.
  Referência: **agenttrace**, baselines locais e checagem de regressão.
