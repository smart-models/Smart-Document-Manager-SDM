import { test, expect } from '@playwright/test';

const pages = ['getting-started', 'cli', 'environment', 'architecture', 'document-pipeline', 'rag-chunking'];
const guide = name => new URL(`../../guide/${name}.html`, import.meta.url).href;

test.beforeEach(async ({ context, page }) => {
  await context.route(/^https?:/, route => {
    throw new Error(`Unexpected network request: ${route.request().url()}`);
  });
  page.on('pageerror', error => { throw error; });
  page.on('console', message => {
    if (message.type() === 'error') throw new Error(message.text());
  });
});

for (const name of pages) {
  test.describe(name, () => {
    test.beforeEach(async ({ page }) => {
      await page.goto(guide(name));
      await page.evaluate(() => document.fonts.ready);
    });

    test('file reading, embedded fonts, keyboard and navigation', async ({ page }) => {
      await expect(page.getByRole('heading', { level: 1 })).toHaveCount(1);
      await expect(page.locator('nav.primary-nav [aria-current="page"]')).toHaveAttribute('href', `${name}.html`);
      await page.keyboard.press('Tab');
      await expect(page.getByRole('link', { name: 'Skip to content' })).toBeFocused();
      await page.keyboard.press('Enter');
      await expect(page.getByRole('main')).toBeFocused();
      expect(await page.evaluate(() => document.fonts.check('600 32px "Barlow Condensed"')
        && document.fonts.check('400 12px "JetBrains Mono"'))).toBe(true);
      expect(await page.locator('[data-copy]').count()).toBeGreaterThan(0);
      for (const button of await page.locator('[data-copy]').all()) {
        await expect(button).not.toHaveAttribute('hidden', '');
      }
      await page.getByRole('navigation', { name: 'Footer' }).getByRole('link', { name: 'Environment', exact: true }).click();
      await expect(page).toHaveURL(guide('environment'));
    });

    test('mixed disclosures survive repeated print preparation', async ({ page }) => {
      await page.locator('details').first().evaluate(item => { item.open = true; });
      const state = () => page.evaluate(() => [...document.querySelectorAll('details')].map(item => item.open));
      const before = await state();
      for (let attempt = 0; attempt < 2; attempt++) {
        await page.evaluate(() => window.dispatchEvent(new Event('beforeprint')));
        expect((await state()).every(Boolean)).toBe(true);
        await page.evaluate(() => window.dispatchEvent(new Event('afterprint')));
        expect(await state()).toEqual(before);
      }
    });

    test('disclosure fragments reveal the addressed answer', async ({ page }) => {
      const target = page.locator('details').last();
      const id = await target.getAttribute('id');
      await target.evaluate(item => { item.open = false; });
      await page.goto(`${guide(name)}#${id}`);
      await expect(target).toHaveAttribute('open', '');
    });

    for (const width of [320, 390, 768, 960, 1024, 1440]) {
      test(`all content reflows at ${width}px`, async ({ page }) => {
        await page.setViewportSize({ width, height: 900 });
        await page.evaluate(() => document.querySelectorAll('details').forEach(item => { item.open = true; }));
        expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
        const commands = page.locator('[data-shell]');
        for (const command of await commands.all()) await expect(command).toBeVisible();
      });
    }

    test('authored content remains readable without JavaScript', async ({ browser }) => {
      const context = await browser.newContext({ javaScriptEnabled: false });
      const page = await context.newPage();
      await page.goto(guide(name));
      await expect(page.getByRole('heading', { level: 1 })).toBeVisible();
      const first = page.locator('details').first();
      for (const disclosure of await page.locator('details').all()) {
        if (await disclosure.getAttribute('open') === null) await disclosure.locator('summary').first().click();
      }
      await expect(first).toHaveAttribute('open', '');
      await expect(page.locator('[data-shell="windows"]').first()).toBeVisible();
      await expect(page.locator('[data-shell="linux"]').first()).toBeVisible();
      await context.close();
    });
  });
}

test('command preview applies only supported switches and retains both shells', async ({ page }) => {
  await page.goto(guide('cli'));
  await page.locator('#mcp-choice').selectOption('off');
  await expect(page.locator('#builder-windows')).toHaveText(String.raw`pwsh .\stack.ps1 up -NoMcp`);
  await expect(page.locator('#builder-linux')).toHaveText('bash stack.sh up --no-mcp');
  await page.locator('#command-choice').selectOption('logs');
  await expect(page.locator('#mcp-choice')).toBeDisabled();
  await expect(page.locator('#wait-choice')).toBeDisabled();
  await expect(page.locator('#builder-windows')).toHaveText(String.raw`pwsh .\stack.ps1 logs -Tail 100`);
  await expect(page.locator('#builder-linux')).toHaveText('bash stack.sh logs --tail 100');
  await page.locator('#command-choice').selectOption('down');
  await expect(page.locator('#builder-linux')).toHaveText('bash stack.sh down');
  await expect(page.locator('[data-shell="windows"]').first()).toBeVisible();
  await expect(page.locator('[data-shell="linux"]').first()).toBeVisible();
});

test('copy falls back to selecting the command when clipboard access fails', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', { value: { writeText: async () => { throw new Error('Denied'); } } });
  });
  await page.goto(guide('getting-started'));
  await page.locator('[data-copy="copy-env-windows"]').click();
  await expect(page.locator('#copy-env-windows-status')).toContainText('command is selected');
  expect(await page.evaluate(() => getSelection().toString())).toContain('Copy-Item');
});

test('environment filtering, hash reveal and print restore hidden search state', async ({ page }) => {
  await page.goto(guide('environment'));
  const total = await page.locator('.parameter').count();
  const search = page.getByRole('searchbox');
  await search.fill('this-setting-does-not-exist');
  await expect(page.locator('#no-results')).toBeVisible();
  await page.evaluate(() => window.dispatchEvent(new Event('beforeprint')));
  expect(await page.locator('.parameter:visible').count()).toBe(total);
  await page.evaluate(() => window.dispatchEvent(new Event('afterprint')));
  expect(await page.locator('.parameter:visible').count()).toBe(0);
  await page.goto(`${guide('environment')}#OCR_VLLM_MODEL_REVISION`);
  await expect(search).toHaveValue('');
  await expect(page.locator('#OCR_VLLM_MODEL_REVISION')).toBeVisible();
  await search.fill('MCP_API_TOKENS');
  await expect(page.locator('#MCP_API_TOKENS')).toBeVisible();
  await expect(page.locator('#parameter-count')).toHaveText(`1 of ${total} settings`);
  await page.getByRole('button', { name: 'Clear search' }).click();
  await expect(search).toHaveValue('');
  await page.locator('[data-query="port"]').click();
  await expect(search).toHaveValue('port');
  await page.getByRole('button', { name: 'Clear search' }).click();
});

test('architecture distinguishes saved JSON from GPU inference', async ({ page }) => {
  await page.goto(guide('architecture'));
  await expect(page.locator('#hero-map-title')).toHaveCount(1);
  await page.locator('#route-choice').selectOption('chunk');
  await expect(page.locator('#request-flow')).toContainText('CPU + tokenizer');
  await expect(page.locator('#request-flow')).not.toContainText('GPU recognition');
  await expect(page.locator('#route-description')).toContainText('no recognition request');
});

test('architecture pipeline player navigates stages and exposes its position', async ({ page }) => {
  await page.goto(guide('architecture'));
  const stages = page.locator('[data-chapter]');
  await expect(stages).toHaveCount(6);
  await expect(stages.nth(0)).toHaveAttribute('aria-pressed', 'true');
  const next = page.getByRole('button', { name: 'Next stage' });
  await next.click();
  await expect(page.locator('#scene-1')).toBeVisible();
  await expect(page.locator('#scene-0')).toBeHidden();
  await expect(stages.nth(1)).toHaveAttribute('aria-pressed', 'true');
  await expect(stages.nth(0)).toHaveAttribute('aria-pressed', 'false');
  await page.locator('#stage-previous').click();
  await expect(page.locator('#scene-0')).toBeVisible();
  await expect(page.locator('#scene-1')).toBeHidden();
  await stages.nth(4).click();
  await expect(page.locator('#scene-4')).toBeVisible();
  await expect(stages.nth(4)).toHaveAttribute('aria-pressed', 'true');
  await expect(next).toBeEnabled();
  await next.click();
  await expect(next).toBeDisabled();
  await page.locator('#restart').click();
  await expect(page.locator('#scene-0')).toBeVisible();
  const valuetext = await page.locator('#timeline').getAttribute('aria-valuetext');
  expect(valuetext).toMatch(/stage 1/i);
});

test('architecture player never auto-advances under reduced motion', async ({ page }) => {
  // playwright.guides.config.js forces reducedMotion: 'reduce'.
  await page.goto(guide('architecture'));
  await page.locator('#play').click();
  await page.waitForTimeout(1000);
  await expect(page.locator('#scene-0')).toBeVisible();
  await expect(page.locator('#scene-1')).toBeHidden();
});

test('architecture scene fragment opens that stage readable', async ({ page }) => {
  await page.goto(`${guide('architecture')}#scene-3`);
  await expect(page.locator('#scene-3')).toBeVisible();
  await expect(page.locator('#scene-0')).toBeHidden();
  await expect(page.locator('#scene-title-3')).toBeVisible();
});

test('architecture read-story and scenes stay readable without JavaScript', async ({ browser }) => {
  const context = await browser.newContext({ javaScriptEnabled: false });
  const page = await context.newPage();
  await page.goto(guide('architecture'));
  await expect(page.locator('.scene')).toHaveCount(6);
  for (const scene of await page.locator('.scene').all()) await expect(scene).toBeVisible();
  await page.locator('#read-story > summary').click();
  await expect(page.locator('#read-story ol li')).toHaveCount(6);
  await context.close();
});

test('architecture explorer reflects archive and agent-access choices', async ({ page }) => {
  await page.goto(guide('architecture'));
  await expect(page.locator('#storage-node')).toBeHidden();
  await expect(page.locator('#mcp-node')).toBeHidden();
  await page.locator('#archive').selectOption('s3');
  await expect(page.locator('#storage-node')).toBeVisible();
  await expect(page.locator('#setup-title')).toContainText('S3 archive');
  await expect(page.locator('#fact-archive')).toContainText('COMPOSE_PROFILES=storage');
  await page.locator('#agent-access').selectOption('mcp');
  await expect(page.locator('#mcp-node')).toBeVisible();
  await expect(page.locator('#setup-title')).toContainText('MCP');
  await page.locator('#archive').selectOption('none');
  await expect(page.locator('#storage-node')).toBeHidden();
  await page.locator('#deployment').selectOption('network');
  await expect(page.locator('#boundary-badge')).toContainText('network');
});

test('getting-started agent prompt copy falls back to opening and selecting the prompt', async ({ page }) => {
  await page.addInitScript(() => {
    Object.defineProperty(navigator, 'clipboard', { value: { writeText: async () => { throw new Error('Denied'); } } });
  });
  await page.goto(guide('getting-started'));
  await page.locator('[data-copy="copy-agent-prompt"]').click();
  await expect(page.locator('#agent-prompt')).toHaveAttribute('open', '');
  await expect(page.locator('#copy-agent-prompt-status')).toContainText('selected');
  expect(await page.evaluate(() => getSelection().toString())).toContain('ocr_check_health');
});

test('getting-started walkthrough advances steps and reports its position', async ({ page }) => {
  await page.goto(guide('getting-started'));
  const nav = page.locator('#tour-nav button');
  await expect(nav).toHaveCount(6);
  await expect(page.locator('#tour-position')).toHaveText('Step 1 of 6');
  await page.locator('#tour-next').click();
  await expect(page.locator('#tour-position')).toHaveText('Step 2 of 6');
  await expect(page.locator('#step-choose')).toBeVisible();
  await expect(page.locator('#step-open')).toBeHidden();
  await expect(nav.nth(1)).toHaveAttribute('aria-current', 'step');
  await page.locator('#tour-previous').click();
  await expect(page.locator('#tour-position')).toHaveText('Step 1 of 6');
  await expect(page.locator('#tour-previous')).toBeDisabled();
});

test('getting-started print reveals every step and restores the active one', async ({ page }) => {
  await page.goto(guide('getting-started'));
  await page.locator('#tour-next').click();
  await expect(page.locator('#step-choose')).toBeVisible();
  await page.evaluate(() => window.dispatchEvent(new Event('beforeprint')));
  expect(await page.locator('.tour-step:visible').count()).toBe(6);
  await page.evaluate(() => window.dispatchEvent(new Event('afterprint')));
  expect(await page.locator('.tour-step:visible').count()).toBe(1);
  await expect(page.locator('#step-choose')).toBeVisible();
});

test('getting-started connect and skill details carry both shells', async ({ page }) => {
  await page.goto(guide('getting-started'));
  for (const id of ['#connect-codex', '#connect-claude-code', '#skill-codex', '#skill-claude-code']) {
    const detail = page.locator(id);
    await expect(detail.locator('[data-shell="windows"]')).toHaveCount(1);
    await expect(detail.locator('[data-shell="linux"]')).toHaveCount(1);
  }
});

test('cli first-start has five steps each with both shells', async ({ page }) => {
  await page.goto(guide('cli'));
  const steps = page.locator('#first-start .journey-list > li');
  await expect(steps).toHaveCount(5);
  for (const step of await steps.all()) {
    await expect(step.locator('[data-shell="windows"]')).toHaveCount(1);
    await expect(step.locator('[data-shell="linux"]')).toHaveCount(1);
  }
});

test('cli builder frontend choice only applies where documented', async ({ page }) => {
  await page.goto(guide('cli'));
  const frontend = page.locator('#frontend-choice');
  await expect(frontend).toBeEnabled();
  await frontend.uncheck();
  await expect(page.locator('#builder-windows')).toHaveText(String.raw`pwsh .\stack.ps1 up -NoFrontend -Wait`);
  await expect(page.locator('#builder-linux')).toHaveText('bash stack.sh up --no-frontend --wait');
  await page.locator('#command-choice').selectOption('logs');
  await expect(frontend).toBeDisabled();
  await expect(page.locator('#builder-linux')).toHaveText('bash stack.sh logs --tail 100');
});

test('environment shell picker dims the other shell without hiding it', async ({ page }) => {
  await page.goto(guide('environment'));
  await page.locator('#shell-choice').selectOption('linux');
  await expect(page.locator('#apply')).toHaveAttribute('data-shell-view', 'linux');
  const windows = page.locator('#apply [data-shell="windows"]').first();
  const linux = page.locator('#apply [data-shell="linux"]').first();
  await expect(windows).toBeVisible();
  await expect(linux).toBeVisible();
  expect(await windows.evaluate(el => getComputedStyle(el).opacity)).toBe('0.45');
  expect(await linux.evaluate(el => getComputedStyle(el).opacity)).toBe('1');
});

test('environment storage section links to documented parameter ids', async ({ page }) => {
  await page.goto(guide('environment'));
  const links = page.locator('#storage a[href^="#OCR_STORAGE"], #storage a[href^="#OCR_S3"], #storage a[href^="#OCR_SEAWEED"], #storage a[href="#COMPOSE_PROFILES"]');
  const hrefs = await links.evaluateAll(items => items.map(item => item.getAttribute('href')));
  expect(hrefs.length).toBeGreaterThan(0);
  for (const href of hrefs) {
    await expect(page.locator(`.parameter${href}`)).toHaveCount(1);
  }
});

for (const name of ['document-pipeline', 'rag-chunking']) {
  test(`${name} player navigates stages and exposes its position`, async ({ page }) => {
    await page.goto(guide(name));
    const stages = page.locator('[data-chapter]');
    await expect(stages).toHaveCount(6);
    await expect(stages.nth(0)).toHaveAttribute('aria-pressed', 'true');
    await page.getByRole('button', { name: 'Next stage' }).click();
    await expect(page.locator('#scene-1')).toBeVisible();
    await expect(page.locator('#scene-0')).toBeHidden();
    await expect(stages.nth(1)).toHaveAttribute('aria-pressed', 'true');
    await page.locator('#stage-previous').click();
    await expect(page.locator('#scene-0')).toBeVisible();
    await page.locator('#restart').click();
    const valuetext = await page.locator('#timeline').getAttribute('aria-valuetext');
    expect(valuetext).toMatch(/stage 1/i);
  });

  test(`${name} player never auto-advances under reduced motion`, async ({ page }) => {
    await page.goto(guide(name));
    await page.locator('#play').click();
    await page.waitForTimeout(1000);
    await expect(page.locator('#scene-0')).toBeVisible();
    await expect(page.locator('#scene-1')).toBeHidden();
  });

  test(`${name} read-story stays readable without JavaScript`, async ({ browser }) => {
    const context = await browser.newContext({ javaScriptEnabled: false });
    const page = await context.newPage();
    await page.goto(guide(name));
    await expect(page.locator('.scene')).toHaveCount(6);
    await page.locator('#read-story > summary').click();
    await expect(page.locator('#read-story ol li')).toHaveCount(6);
    await context.close();
  });
}

test('document-pipeline lab filters furniture, shows relations and swaps samples', async ({ page }) => {
  await page.goto(guide('document-pipeline'));
  const blockList = page.locator('#block-list li');
  const total = await blockList.count();
  expect(total).toBeGreaterThan(0);
  await page.locator('#hide-furniture').check();
  const remaining = await blockList.count();
  expect(remaining).toBeLessThan(total);
  for (const row of await blockList.all()) {
    expect(['header', 'footer', 'number', 'aside_text']).not.toContain(await row.getAttribute('data-type'));
  }
  await page.locator('#hide-furniture').uncheck();
  await expect(page.locator('#block-list .block-rel').first()).toContainText('caption of block');
  await page.locator('#show-relations').uncheck();
  await expect(page.locator('#block-list .block-rel')).toHaveCount(0);
  await page.locator('#show-relations').check();
  for (const sample of ['paper', 'form', 'invoice']) {
    await page.locator('#sample').selectOption(sample);
    await expect(page.locator('#page-title')).not.toBeEmpty();
    await expect(page.locator('#page-figure [data-block-type]')).not.toHaveCount(0);
    await expect(page.locator('#markdown-preview')).not.toBeEmpty();
  }
});

test('rag-chunking budget lab keeps the protected unit whole at every budget', async ({ page }) => {
  await page.goto(guide('rag-chunking'));
  const count = async () => parseInt((await page.locator('#chunk-count').textContent()), 10);
  await page.locator('#budget').fill('256');
  const at256 = await count();
  await page.locator('#budget').fill('512');
  const at512 = await count();
  expect(at512).toBeLessThan(at256);
  for (const value of ['128', '256', '512', '1024']) {
    await page.locator('#budget').fill(value);
    const chunks = page.locator('#chunk-list > li');
    const hits = await chunks.evaluateAll(items =>
      items.filter(item => item.querySelector('[data-unit="u4"]')).length);
    expect(hits).toBe(1);
  }
  await expect(page.locator('#budget')).toHaveAttribute('aria-valuetext', /tokens/);
  await page.locator('#overlap-toggle').check();
  await expect(page.locator('.chunk-overlap').first()).toContainText('overlap');
  await page.locator('#overlap-toggle').uncheck();
  await expect(page.locator('.chunk-overlap')).toHaveCount(0);
  await page.locator('#document').selectOption('minutes');
  await expect(page.locator('#chunk-count')).toContainText('chunk');
});
