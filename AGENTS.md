# Instruções para agentes (Copilot, Claude Code e outros)

Leia este arquivo inteiro antes de qualquer tarefa. O design completo está em `docs/design.md`
e o backlog em `docs/tarefas.md`.

## O projeto

`token-controler` é um CLI em Python que lê os logs locais de ferramentas de AI (Claude Code,
GitHub Copilot no VS Code) e grava métricas de uso e custo num SQLite local:
tokens, cache, chamadas, tool calls, erros, tempo de sessão e custo estimado.

Ele roda num notebook corporativo, com dados sensíveis. Por isso as regras abaixo são inegociáveis.

## Regras inegociáveis

1. **Nunca gravar texto de conversa.** O banco recebe só números e metadados: modelo, tokens,
   nome da tool, flags de erro, timestamps, IDs. Prompts, respostas, resultados de tools,
   argumentos de tools e caminhos de arquivos editados são descartados na leitura.
   Para identificar um arquivo (one-shot, leituras repetidas), grave um hash do caminho, nunca o caminho.
   **Única exceção, aprovada pelo dono do projeto em 21/11/2026:** na leitura de cada prompt, o texto pode ser
   examinado em memória para derivar o rótulo fixo `debug` (`token_controler/rotulos.py`), e só o rótulo é gravado
   (`eventos.rotulo`). O texto nunca é gravado, impresso, logado nem passado a outro código além do rotulador.
   Não crie outros rótulos derivados de prompt sem nova aprovação explícita.
   **Segunda exceção, aprovada pelo dono do projeto em 22/11/2026, em modo de teste (T19):** o NOME do arquivo tocado
   por uma tool (`analise.py`), sem a pasta, pode ser gravado em `tool_calls.alvo_nome`. Antes de gravar, remova qualquer
   pasta (separadores `/` e `\`) e limite o tamanho. Continua PROIBIDO gravar caminho completo, comando de shell,
   argumentos de tools e qualquer conteúdo; o `alvo_hash` do caminho completo continua, para distinguir arquivos de
   mesmo nome em pastas diferentes. A chave `gravar_nome_arquivo` (padrão `true`) desliga a gravação e `tc limpar-nomes`
   apaga a coluna. Comandos de shell só aparecem na tela com `tc sessao --comandos`, lidos do log original na hora, em
   memória; nunca são gravados.
   **Terceira exceção, aprovada pelo dono do projeto em 22/11/2026, em modo de teste (T19):** nas tools de leitura de
   arquivo (`Read`), os DOIS NÚMEROS do trecho lido, a linha inicial e a quantidade de linhas (`offset` e `limit`), podem
   ser gravados em `tool_calls.leitura_ini` e `leitura_qtd`, para distinguir reler o mesmo trecho de ler outro trecho do
   arquivo. Só números inteiros; nenhum outro argumento (`pages`, padrões de busca, textos) é gravado. A chave
   `gravar_trecho_leitura` (padrão `true`) desliga a gravação e `tc limpar-trechos` apaga as colunas.
2. **Nunca fazer chamada de rede.** Nada de `requests`, `urllib`, download de tabela de preços ou telemetria.
   Os preços ficam em `token_controler/precos.py`, editados à mão.
3. **Só biblioteca padrão do Python 3.9+.** Sem dependências de runtime. `pytest` é a única dependência de desenvolvimento.
   Sintaxe compatível com 3.9: use `from __future__ import annotations`, sem `match`, sem `X | Y` fora de anotações.
   **Única exceção, aprovada pelo dono do projeto em 21/11/2026:** `rich`, usado só em `token_controler/painel.py`
   (o painel do `tc painel`, a tela do `tc live` e a árvore do `tc sessao`). O `import rich` fica só nesse módulo, que o
   `cli.py` importa de forma preguiçosa; os demais comandos continuam só com a biblioteca padrão e, se o `rich` faltar, esses
   três avisam e caem em texto simples (`--simples`).
   Não adicione outras dependências sem nova aprovação explícita.
4. **Nunca estimar tokens a partir do texto.** Sem contagem real, o valor fica `NULL` ou é marcado "sem dados".
5. **Só leitura das fontes.** Nunca modificar, mover ou apagar os arquivos de log das ferramentas.
6. **Nunca commitar dados reais.** Logs reais, `*.db` e dumps ficam fora do git. Testes usam
   só fixtures sintéticas em `tests/fixtures/`, escritas à mão e sem nenhum conteúdo real.
7. **Todo número diz de onde veio.** Custo com `origem_custo` (`registrado`, `calculado`, `sem_preco`);
   sessão com `nivel_detalhe` (`completo`, `agregado`, `limitado`). Modelo sem preço nunca recebe valor chutado.

## Arquitetura

```
token_controler/
  cli.py              # comandos `tc` (argparse); textos em português
  painel.py           # `tc painel`, tela do `tc live` e árvore do `tc sessao` (única parte que usa `rich`)
  agora.py            # dados e texto simples da tela do `tc live` (só biblioteca padrão)
  detalhe.py          # dados e árvore neutra do `tc sessao` (só biblioteca padrão; reusa as regras de diagnostico.py)
  db.py               # schema SQLite e conexão
  precos.py           # tabela de preços (USD por milhão de tokens)
  fontes/
    claude_code.py    # adaptador: ~/.claude/projects/**/*.jsonl
    copilot.py        # (Fase 2) adaptador do Copilot no VS Code
```

- **Um adaptador por ferramenta**, em `fontes/`, isolado. Cada adaptador expõe
  `ingerir(con, pasta=None, tudo=False, apelidos=None) -> dict` e grava no **schema único**
  (`chamadas`, `tool_calls`, `eventos`, `arquivos`), preenchendo a coluna `ferramenta`.
- Os relatórios no `cli.py` não sabem de qual ferramenta veio o dado.
- Ingestão incremental: um arquivo só é reprocessado se `tamanho` ou `mtime` mudarem; ao reprocessar,
  apaga as linhas daquele arquivo e grava de novo.
- Uma fonte só entra se tiver timestamp, ID de sessão, modelo e contagem de tokens (ou custo registrado).

## Como trabalhar

- Antes de mudar código, leia `docs/design.md` e a tarefa correspondente em `docs/tarefas.md`.
- Faça mudanças pequenas e focadas numa tarefa por vez.
- **Todo adaptador ou métrica nova precisa de teste** com fixture sintética. Rode `python -m pytest` antes de terminar.
- Ao concluir uma tarefa, marque-a em `docs/tarefas.md` e atualize o `README.md` se a interface mudou.
- Se a tarefa exigir conhecer um formato de log que você não viu, **não invente**: pare e peça uma amostra
  da estrutura (gerada por `scripts/inspecionar_copilot.py`, que mostra só campos e tipos).
- **Referências:** quando a tarefa em `docs/tarefas.md` tiver uma linha "Referência", leia o projeto
  indicado em `../referencias/` (CodeBurn, ccusage ou AgentTrace, todos MIT) **antes de planejar**, sem
  esperar que o prompt peça. Siga o procedimento da seção "Referências" de `docs/tarefas.md`: listar o
  que aproveita e o que descarta, reimplementar em Python (não traduzir) e dar crédito em `NOTICE.md`
  quando a adaptação for próxima do original.

## Comandos

```bash
pip install -e ".[dev]"     # instala em modo desenvolvimento, com pytest
python -m pytest            # testes
tc ingest && tc resumo      # uso real
```

## Estilo

- Nomes de funções, variáveis e mensagens do CLI em português, como o código existente.
- Funções pequenas, com docstring curta. SQL legível, em strings com aspas triplas.
