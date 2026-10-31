import { readFile } from "node:fs/promises";
import { expect, test, openHealthyConsole, IMAGE, PARSE, OCR } from "./fixtures.mjs";

const attention = String.raw`Attention(Q,K,V)=softmax(\frac{QK^{T}}{\sqrt{d_{k}}})V`;
const excerpt = `### 3.2.1 Scaled Dot-Product Attention\n\n` +
  String.raw`Queries and keys have dimension $ d_{k} $, and values $ d_{v} $. ` +
  String.raw`Divide the dot products by $ \sqrt{d_{k}} $.` +
  `\n\n$$ ${attention} $$\n\n` +
  String.raw`Scale by $ \frac{1}{\sqrt{d_k}} $. Extremely small gradients $ ^{4} $.`;

async function parseMarkdown(page, markdown) {
  const payload = { ...PARSE, pages: [{ page: 1, markdown }] };
  await page.route("**/parse", route => route.fulfill({ json: payload }));
  await openHealthyConsole(page);
  await page.locator("#parse-file").setInputFiles(IMAGE);
  await page.locator("#parse-run").click();
  await expect(page.locator("#parse-results .metric-row")).toContainText("processing time");
  return payload;
}

test("renders the attention excerpt with local fonts and preserves its source", async ({ page }, testInfo) => {
  const external = [];
  const failures = [];
  page.on("request", request => {
    if (!request.url().startsWith("http://127.0.0.1:8129/") && !request.url().startsWith("data:")) external.push(request.url());
  });
  page.on("response", response => { if (response.status() >= 400) failures.push(response.url()); });
  await parseMarkdown(page, excerpt);
  const preview = page.locator("#parse-results .result-text");
  await expect(preview.locator(".katex")).toHaveCount(6);
  await expect(preview.locator(".katex-display")).toHaveCount(1);
  await expect(preview.locator(".katex-display math mfrac")).toHaveCount(1);
  await expect(preview.locator(".katex-display math msqrt")).toHaveCount(1);
  await expect(preview.locator(".katex-display annotation")).toHaveText(attention);
  await page.locator("#parse-results").getByRole("tab", { name: "markdown" }).click();
  expect(await page.locator("#parse-results .pv-raw code").textContent()).toBe(excerpt);
  await expect(page.locator("#parse-results .pv-raw .katex")).toHaveCount(0);
  await page.locator("#parse-results").getByRole("tab", { name: "rendered" }).click();
  await page.evaluate(() => document.fonts.ready);
  expect(await preview.locator(".katex").first().evaluate(node => getComputedStyle(node).fontFamily)).toContain("KaTeX_Main");
  expect(await page.evaluate(() => [...document.fonts].some(font => font.family === "KaTeX_Main" && font.status === "loaded"))).toBe(true);
  for (const width of [1280, 390]) {
    await page.setViewportSize({ width, height: 1000 });
    expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
    await page.locator("#parse-results").screenshot({ path: testInfo.outputPath(`attention-${width}.png`) });
  }
  expect(external).toEqual([]);
  expect(failures).toEqual([]);
});

test("protects all math delimiters, matrix rows, comparisons and table cells from Markdown", async ({ page }) => {
  const markdown = String.raw`Inline \(a_i+b_j\), $ c_k $, and $$x^2$$.

\[
A=\begin{pmatrix}1 & 2 \\ 3 & 4\end{pmatrix}
\]

$$x<y$$

| Formula | Meaning |
| --- | --- |
| $d_k$ | dimension |`;
  await parseMarkdown(page, markdown);
  const preview = page.locator("#parse-results .result-text");
  await expect(preview.locator(".katex")).toHaveCount(6);
  await expect(preview.locator("math mtable mtr")).toHaveCount(2);
  await expect(preview.locator("math mtable mtd")).toHaveCount(4);
  await expect(preview.locator("table td .katex")).toHaveCount(1);
  expect(await preview.locator("annotation").allTextContents()).toContain("a_i+b_j");
  expect(await preview.locator("annotation").allTextContents()).toContain("x<y");
});

test("leaves code, escaped dollars, prices and incomplete formulas readable", async ({ page }) => {
  const markdown = "`$code_i$`\n\n```latex\n\\[code_j\\]\n```\n\n" +
    String.raw`Cost: \$5 and \$10. Also $5 and $10. Unclosed $x_i.` +
    "\n\n" + String.raw`$$\frac{1}{$$`;
  await parseMarkdown(page, markdown);
  const preview = page.locator("#parse-results .result-text");
  await expect(preview.locator(".katex")).toHaveCount(0);
  await expect(preview.locator("code")).toHaveText(["$code_i$", "\\[code_j\\]\n"]);
  await expect(preview).toContainText("Cost: $5 and $10. Also $5 and $10. Unclosed $x_i.");
  await expect(preview).toContainText(String.raw`$$\frac{1}{$$`);
});

test("wide display formulas scroll inside their preview on mobile", async ({ page }) => {
  await page.setViewportSize({ width: 390, height: 900 });
  await parseMarkdown(page, `$$${Array.from({ length: 30 }, (_, index) => `x_{${index}}`).join("+")}$$`);
  const formula = page.locator("#parse-results .math-display");
  await expect(formula.locator(".katex")).toHaveCount(1);
  expect(await formula.evaluate(node => node.scrollWidth > node.clientWidth)).toBe(true);
  expect(await formula.evaluate(node => getComputedStyle(node).overflowX)).toBe("auto");
  expect(await page.evaluate(() => document.documentElement.scrollWidth <= innerWidth)).toBe(true);
});

for (const [name, result] of [
  ["display", String.raw`\[E=m c^{2}\]`],
  ["bare", String.raw`x=\frac{-b\pm\sqrt{b^2-4ac}}{2a}`],
]) {
  test(`previews ${name} formula OCR and downloads its text and raw JSON`, async ({ page }) => {
    const payload = { ...OCR, task: "formula", result };
    await page.route("**/ocr", route => route.fulfill({ json: payload }));
    await openHealthyConsole(page, "#ocr");
    await page.locator("#ocr-task").selectOption("formula");
    await page.locator("#ocr-file").setInputFiles(IMAGE);
    await page.locator("#ocr-download").check();
    const downloads = [];
    page.on("download", item => downloads.push(item));
    await page.locator("#ocr-run").click();
    await expect.poll(() => downloads.length).toBe(2);
    expect(downloads.map(item => item.suggestedFilename()).sort()).toEqual(["sample.ocr.json", "sample.ocr.md"]);
    const download = downloads.find(item => item.suggestedFilename().endsWith(".json"));
    const text = downloads.find(item => item.suggestedFilename().endsWith(".md"));
    expect(await readFile(await text.path(), "utf8")).toBe(result);
    await expect(page.locator("#ocr-results .formula-preview .katex-display")).toHaveCount(1);
    await page.locator('#ocr-results [data-tab="markdown"]').click();
    await expect(page.locator('#ocr-results [data-panel="markdown"] code')).toHaveText(result);
    expect(JSON.parse(await readFile(await download.path(), "utf8"))).toEqual(payload);
  });
}

test("malformed and untrusted TeX cannot execute HTML or erase valid surrounding content", async ({ page }) => {
  const markdown = String.raw`Before $x^2$.

$$\notARealCommand{<img src=x onerror=window.__mathXss=true>}$$

$$\href{javascript:window.__mathXss=true}{click}$$

$$\includegraphics{https://example.invalid/track.png}$$

$$\htmlStyle{position:fixed}{bad}$$

$$\def\a{\a}\a$$

After $y^2$. <script>window.__mathXss=true</script>`;
  const external = [];
  page.on("request", request => { if (request.url().includes("example.invalid")) external.push(request.url()); });
  await parseMarkdown(page, markdown);
  const preview = page.locator("#parse-results .result-text");
  await expect(preview).toContainText("Before");
  await expect(preview).toContainText("After");
  await expect(preview.locator(".math-fallback")).toHaveCount(2);
  await expect(preview.locator("script, img, [onerror], [style*='position:fixed'], a[href]")).toHaveCount(0);
  expect(await page.evaluate(() => window.__mathXss)).toBeUndefined();
  expect(external).toEqual([]);
});
