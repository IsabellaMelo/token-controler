# Testes visuais do `tc web`

Ferramenta **só de desenvolvimento** (Node + Playwright). O `tc` continua rodando apenas com Python e a biblioteca
padrão; nada daqui é importado pelo pacote. Ver a exceção na regra 3 do `AGENTS.md`.

Os testes sobem sozinhos o `tc web --demo` (banco fictício, horário congelado em 22/11/2026 11:07:42, sem recarregar) e
derrubam no fim. Por isso dois prints da mesma versão são idênticos.

## O que é testado

- `hoje.spec.ts`
  - print da tela Hoje (`toHaveScreenshot`) em 1440 px (`desktop-1440`) e 390 px (`celular-390`);
  - página sem rolagem horizontal;
  - sem `<script>`, sem `<meta refresh>` no modo demo, CSP `default-src 'none'`;
  - navegação: aba atual, abas de período ainda desligadas (T25), chips de filtro, link da sessão e volta;
  - em todos os testes: falha se a página pedir qualquer coisa fora de `http://127.0.0.1:8799` (a requisição é
    bloqueada e anotada) ou se aparecer erro ou aviso no console.

## Rodar

```powershell
cd tests\visual
npm ci                                   # uma vez
$env:TC_PYTHON = ".venv\Scripts\python.exe"   # Python com o tc, relativo à raiz do repositório (padrão: python)
npx playwright test                       # local: usa o Edge instalado (channel msedge)
npx playwright show-report                # relatório com as diferenças, se algo falhar
```

No CI (`CI=true`) o navegador é o Chromium do Playwright, num runner Windows.

## Prints de referência

Ficam em `__prints__/<largura>/hoje.png`, iguais para o Edge e o Chromium (tolerância de 1% dos pixels).
Só se atualizam depois de olhar a tela e aprovar a mudança:

```powershell
npx playwright test --update-snapshots
```

e commitar os PNGs junto com a mudança visual.
