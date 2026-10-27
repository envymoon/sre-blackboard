// First-screen JavaScript budget (plan, M9): for each page a visitor can land on, the entry
// chunk plus that route's chunk and everything they import statically, gzipped, under 200 KB.
// Reads Vite's manifest, so it counts what the browser really downloads before first paint.

import { readFileSync } from "node:fs";
import { gzipSync } from "node:zlib";

const BUDGET = 200 * 1024;
const ROUTES = {
  landing: ["src/pages/Landing.tsx"],
  replay: ["src/app/ReplayApp.tsx"],
  "incident (console)": ["src/app/LiveApp.tsx", "src/features/incident/IncidentPage.tsx"],
  login: ["src/app/Login.tsx"],
};

const manifest = JSON.parse(readFileSync("dist/.vite/manifest.json", "utf8"));

function closure(keys, seen = new Set()) {
  for (const k of keys) {
    if (seen.has(k)) continue;
    seen.add(k);
    closure(manifest[k]?.imports ?? [], seen);
  }
  return seen;
}

const gz = (file) => gzipSync(readFileSync(`dist/${file}`)).length;
let failed = false;
for (const [name, entries] of Object.entries(ROUTES)) {
  const files = [...closure(["index.html", ...entries])].map((k) => manifest[k]?.file).filter((f) => f?.endsWith(".js"));
  const total = files.reduce((n, f) => n + gz(f), 0);
  const ok = total <= BUDGET;
  failed ||= !ok;
  console.log(`${ok ? "ok  " : "OVER"} ${name.padEnd(20)} ${(total / 1024).toFixed(1).padStart(6)} KB gzip (${files.length} files)`);
}
process.exit(failed ? 1 : 0);
