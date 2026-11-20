// Renders the Mermaid diagrams in docs/architecture.md to docs/assets/architecture-<n>.png,
// the pictures in the README. Not part of the build; run it after editing the diagrams:
//
//   cd web && npm i --no-save mermaid@11.12.0 playwright && node scripts/render-diagrams.mjs
//
// Playwright needs a Chromium (npx playwright install chromium, or CHROMIUM=/path/to/chrome).
import { readFileSync } from "node:fs";
import { createRequire } from "node:module";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const repo = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const mermaid = createRequire(import.meta.url).resolve("mermaid/dist/mermaid.min.js");
const diagrams = [...readFileSync(`${repo}/docs/architecture.md`, "utf8").matchAll(/```mermaid\n([\s\S]*?)```/g)].map(
  (m) => m[1],
);

const browser = await chromium.launch(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {});
const page = await browser.newPage({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 2 });
await page.setContent(
  '<body style="margin:0;background:#fff"><div id="out" style="display:inline-block;padding:24px"></div></body>',
);
await page.addScriptTag({ path: mermaid });
for (const [i, source] of diagrams.entries()) {
  await page.evaluate(async (src) => {
    const m = globalThis.mermaid;
    m.initialize({
      startOnLoad: false,
      theme: "neutral",
      fontFamily: "Inter, Helvetica, Arial, sans-serif",
      flowchart: { useMaxWidth: false, wrappingWidth: 460 },
      state: { useMaxWidth: false },
    });
    const { svg } = await m.render(`d${Date.now()}`, src);
    document.getElementById("out").innerHTML = svg;
  }, source);
  const path = `${repo}/docs/assets/architecture-${i + 1}.png`;
  await page.locator("#out").screenshot({ path });
  console.log(path);
}
await browser.close();
