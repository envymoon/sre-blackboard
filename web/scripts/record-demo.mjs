// Records docs/assets/demo-bad-release.gif: the replay of the real bad-release incident from the
// lab (public/cases/bad-release.json) at 30×, from the alert to RESOLVED, approving the plan on
// the way like the recorded approver did. No server and no model: it is the Pages build.
//
//   cd web && npm i --no-save playwright && VITE_SITE=pages npm run build
//   VITE_SITE=pages npx vite preview --port 4173 &
//   node scripts/record-demo.mjs        # needs ffmpeg on PATH
import { execFileSync } from "node:child_process";
import { mkdtempSync, readdirSync } from "node:fs";
import { tmpdir } from "node:os";
import { dirname, join, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { chromium } from "playwright";

const repo = resolve(dirname(fileURLToPath(import.meta.url)), "../..");
const url = process.env.DEMO_URL ?? "http://localhost:4173/sre-blackboard/#/replay/bad-release";
const dir = mkdtempSync(join(tmpdir(), "demo-"));

const browser = await chromium.launch(process.env.CHROMIUM ? { executablePath: process.env.CHROMIUM } : {});
const ctx = await browser.newContext({
  viewport: { width: 1280, height: 760 },
  colorScheme: "dark",
  recordVideo: { dir, size: { width: 1280, height: 760 } },
});
const page = await ctx.newPage();
await page.goto(url);
await page.getByRole("button", { name: "Play" }).waitFor();
await page.waitForTimeout(1200);
await page.getByRole("button", { name: "30×" }).click();
await page.getByRole("button", { name: "Play" }).click();
// The replay stops where the real run waited for a human; the viewer plays the approver.
const approve = page.getByRole("button", { name: /Approve plan/ });
await approve.waitFor({ timeout: 120_000 });
await approve.scrollIntoViewIfNeeded();
await page.waitForTimeout(2500);
await approve.click();
await page.getByRole("button", { name: "Replay", exact: true }).waitFor({ timeout: 180_000 });
// End on the audited timeline: diagnosis, plan, policy, approval, execution, SLO checks.
await page.waitForTimeout(1500);
await page.mouse.move(800, 500);
for (let i = 0; i < 24; i++) {
  await page.mouse.wheel(0, 90);
  await page.waitForTimeout(120);
}
await page.waitForTimeout(2500);
await ctx.close();
await browser.close();

const video = join(
  dir,
  readdirSync(dir).find((f) => f.endsWith(".webm")),
);
const filter =
  "fps=6,scale=800:-1:flags=lanczos,split[a][b];[a]palettegen=max_colors=64:stats_mode=diff[p];" +
  "[b][p]paletteuse=dither=none:diff_mode=rectangle";
execFileSync("ffmpeg", [
  "-loglevel",
  "error",
  "-y",
  "-i",
  video,
  "-vf",
  filter,
  `${repo}/docs/assets/demo-bad-release.gif`,
]);
console.log(`${repo}/docs/assets/demo-bad-release.gif`);
