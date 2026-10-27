import { readFileSync } from "node:fs";
import { describe, expect, it } from "vitest";

// WCAG AA for the text tokens on the surfaces they sit on, in both themes.
const css = readFileSync("src/styles/tokens.css", "utf8");
const tokens = Object.fromEntries(
  [...css.matchAll(/(--c-[\w-]+): light-dark\((#[0-9a-f]{6}), (#[0-9a-f]{6})\);/g)].map((m) => [
    m[1]!,
    [m[2]!, m[3]!] as const,
  ]),
);

function luminance(hex: string): number {
  const c = [1, 3, 5].map((i) => parseInt(hex.slice(i, i + 2), 16) / 255);
  const [r, g, b] = c.map((v) => (v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4)) as [number, number, number];
  return 0.2126 * r + 0.7152 * g + 0.0722 * b;
}

function contrast(a: string, b: string): number {
  const [x, y] = [luminance(a), luminance(b)].sort((p, q) => q - p) as [number, number];
  return (x + 0.05) / (y + 0.05);
}

const TEXT = ["--c-fg", "--c-fg-2", "--c-muted", "--c-faint", "--c-accent-text"];
const SURFACES = ["--c-bg", "--c-rail", "--c-panel", "--c-sunken", "--c-raised"];
const PAIRS: [string, string][] = [
  ...TEXT.flatMap((t) => SURFACES.map((s) => [t, s] as [string, string])),
  ["--c-accent-fg", "--c-accent"],
  ["--c-accent-fg", "--c-accent-hover"],
  ["--c-accent-text", "--c-accent-soft"],
  ...["success", "warning", "danger", "critical"].map((t) => [`--c-${t}`, `--c-${t}-soft`] as [string, string]),
  ...["success", "warning", "danger", "critical"].map((t) => [`--c-${t}`, "--c-panel"] as [string, string]),
  ["--c-del-fg", "--c-del"],
  ["--c-add-fg", "--c-add"],
];

describe("tokens", () => {
  for (const [theme, i] of [
    ["light", 0],
    ["dark", 1],
  ] as const) {
    it.each(PAIRS)(`${theme}: %s on %s is at least 4.5:1`, (fg, bg) => {
      expect(tokens[fg], fg).toBeDefined();
      expect(tokens[bg], bg).toBeDefined();
      expect(contrast(tokens[fg]![i], tokens[bg]![i])).toBeGreaterThanOrEqual(4.5);
    });
  }
});
