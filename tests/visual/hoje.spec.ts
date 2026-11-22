// Tela Hoje do `tc web --demo`: print de referência, navegação e nenhuma requisição fora de 127.0.0.1.
import { test as base, expect } from '@playwright/test';
import { BASE } from './playwright.config';

// Guarda automática em todos os testes: qualquer requisição que não seja do próprio tc web (ou data:) é bloqueada e faz o
// teste falhar; erro ou aviso no console também.
const test = base.extend<{ guarda: void }>({
  guarda: [
    async ({ page }, use) => {
      const fora: string[] = [];
      const console_: string[] = [];
      await page.route('**/*', (rota) => {
        const url = rota.request().url();
        if (url.startsWith(`${BASE}/`) || url.startsWith('data:')) return rota.continue();
        fora.push(url);
        return rota.abort('blockedbyclient');
      });
      page.on('console', (m) => {
        if (m.type() === 'error' || m.type() === 'warning') console_.push(`${m.type()}: ${m.text()}`);
      });
      page.on('pageerror', (e) => console_.push(`pageerror: ${e.message}`));
      await use();
      expect(fora, 'requisições para fora de 127.0.0.1').toEqual([]);
      expect(console_, 'mensagens de erro no console').toEqual([]);
    },
    { auto: true },
  ],
});

test('Hoje: print de referência', async ({ page }) => {
  await page.goto('/');
  await expect(page.locator('[data-kpi=chamadas]')).toHaveText('382');
  await expect(page).toHaveScreenshot('hoje.png', { fullPage: true });
});

test('Hoje: sem rolagem horizontal da página', async ({ page }) => {
  await page.goto('/');
  const larguras = await page.evaluate(() => ({
    pagina: document.documentElement.scrollWidth,
    tela: document.documentElement.clientWidth,
  }));
  expect(larguras.pagina).toBeLessThanOrEqual(larguras.tela);
});

test('Hoje: sem JavaScript, sem recarregar no modo demo e só recursos locais', async ({ page }) => {
  const resposta = await page.goto('/');
  expect(resposta?.status()).toBe(200);
  expect(resposta?.headers()['content-security-policy']).toContain("default-src 'none'");
  await expect(page.locator('script')).toHaveCount(0);
  await expect(page.locator('meta[http-equiv="refresh"]')).toHaveCount(0);
});

test('Hoje: abas, filtros e link para a sessão', async ({ page }) => {
  await page.goto('/');
  // aba atual; as de período (T25) ainda não são links
  await expect(page.locator('.abas a[aria-current="page"]')).toHaveText('Hoje');
  for (const nome of ['7 dias', '30 dias', 'Tudo']) {
    await expect(page.locator('.abas a', { hasText: nome })).toHaveCount(0);
    await expect(page.locator('.abas .aba-off', { hasText: nome })).toHaveAttribute('aria-disabled', 'true');
  }
  await expect(page.locator('.filtros .chip')).toHaveText(['Ferramenta: Claude Code', 'Projeto: todos', 'Modelo: todos']);

  // link da sessão: muda a URL e o conteúdo; "Voltar" traz a tela Hoje de volta
  const sessao = page.locator('a.sessao-link').nth(1);
  await expect(sessao).toHaveText('analisador-curriculos');
  await sessao.click();
  await expect(page).toHaveURL(`${BASE}/sessao/demo0002`);
  await expect(page.locator('h1')).toHaveText('Detalhe da sessão');
  await expect(page.locator('.recado')).toContainText('tc sessao demo0002');
  await page.getByRole('link', { name: 'Voltar para Hoje' }).click();
  await expect(page).toHaveURL(`${BASE}/`);
  await expect(page.locator('h2#t-sess')).toHaveText('Sessões de hoje');
});
