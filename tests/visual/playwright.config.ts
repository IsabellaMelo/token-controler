// Testes visuais do `tc web` (ferramenta de desenvolvimento; o tc em si roda só com Python + biblioteca padrão).
// Sobe o `tc web --demo` sozinho: banco fictício e horário congelado, então dois prints da mesma versão são idênticos.
import { defineConfig } from '@playwright/test';

const CI = !!process.env.CI;
export const PORTA = 8799;
export const BASE = `http://127.0.0.1:${PORTA}`;

export default defineConfig({
  testDir: '.',
  testMatch: '*.spec.ts',
  // um print de referência por tela e largura, sem o nome do navegador e do sistema: o Edge (local) e o Chromium (CI)
  // usam o mesmo Blink e as mesmas fontes do Windows, e a tolerância abaixo cobre o antisserrilhado
  snapshotPathTemplate: '{testDir}/__prints__/{projectName}/{arg}{ext}',
  workers: 1,
  forbidOnly: CI,
  reporter: CI ? [['list'], ['html', { open: 'never' }]] : [['list']],
  expect: {
    toHaveScreenshot: { maxDiffPixelRatio: 0.01, animations: 'disabled', caret: 'hide', scale: 'css' },
  },
  use: {
    baseURL: BASE,
    browserName: 'chromium',
    channel: CI ? undefined : 'msedge',   // local: o Edge instalado; CI: o Chromium do Playwright
    colorScheme: 'dark',
    locale: 'pt-BR',
  },
  projects: [
    { name: 'desktop-1440', use: { viewport: { width: 1440, height: 900 } } },
    { name: 'celular-390', use: { viewport: { width: 390, height: 844 } } },
  ],
  webServer: {
    // TC_PYTHON: o Python com o tc, relativo à raiz do repositório (ex.: .venv\Scripts\python.exe); padrão: `python`
    command: `${process.env.TC_PYTHON || 'python'} -m token_controler web --demo --porta ${PORTA}`,
    cwd: '../..',
    url: `${BASE}/`,
    reuseExistingServer: !CI,
    timeout: 30_000,
    env: { PYTHONUTF8: '1' },
  },
});
