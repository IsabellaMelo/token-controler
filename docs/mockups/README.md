# Mockups do `tc web`

Referência visual das três telas do `tc web` (design.md §8.1). **Os dados são fictícios.**

| Arquivo | Tela | Rota no `tc web` |
|---|---|---|
| `hoje.html` | Visão do dia: KPIs, gasto por hora, custo por tipo de token, por projeto/modelo/tool, sessões | `/` (`?periodo=7d`, `?projeto=x`...) |
| `sessao.html` | Detalhe de uma sessão: KPIs, chamadas por minuto, diagnóstico, tokens e tools | `/sessao/<id>` |
| `sequencia.html` | Sequência da sessão: turnos e passos em ordem, com duração e erros | `/sessao/<id>/sequencia?turno=N&tipo=...` |

## Como usar

- Abra no navegador para ver. Os links entre as telas funcionam.
- São só HTML + CSS: nenhum JavaScript, nenhum recurso externo, fontes do sistema. É o mesmo conjunto de restrições do `tc web`.
- O objetivo é **reproduzir o visual** com os dados reais, gerados pelo Python no servidor. Não copie os números.
- No `tc web` de verdade:
  - acrescentar `<meta http-equiv="refresh" content="5">`;
  - trocar os links `#topo` (abas, filtros, turnos) por URLs com parâmetros;
  - mover o CSS repetido dos atributos `style` para uma folha de estilo comum (classes), servida pelo próprio `tc web` ou embutida num `<style>`.

## O que cada elemento representa

- **Barras** (gasto por hora, projeto, modelo, tool, tempo ativo, taxa de cache): `div` com largura ou altura em `%` ou `px` calculada pelo Python.
- **Rosca** (custo por tipo de token): `conic-gradient` com as porcentagens.
- **Sparkline** (custo acumulado): `<polyline>` em SVG com os pontos calculados pelo Python.
- **Linha do tempo de passos** (sequência): barra posicionada com `left` e `width` em `%` da duração do turno.
- **Cores por tipo de passo:** P prompt, M modelo, R/G leitura, E escrita, $ shell, MCP, SA subagente. Borda vermelha = falhou. Bloco avermelhado = loop de retry.

## Diferenças conhecidas em relação aos dados reais

- A tool de subagente nos logs se chama `Agent` (ou `Task` em versões antigas).
- A tela de sequência mostra a extensão do arquivo junto do hash (`#a3f2 · .py`), mas `alvo_ext` **não foi aprovado**. Mostre só o hash até a decisão.
- Recusas suas (`recusado_usuario`) não aparecem no mockup. No `tc web`, elas não contam como erro e podem aparecer como um passo neutro ("recusada").
- `duracao_ms` inclui a espera pela aprovação. Enquanto isso não for separado, o selo "tool lenta" não deve aparecer em tools que passaram por aprovação.
