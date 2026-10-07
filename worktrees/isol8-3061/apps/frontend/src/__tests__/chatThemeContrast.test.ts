import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * Recomputes WCAG contrast for every text token against every plane it can
 * sit on, in all four theme states.
 *
 * This exists because the first cut of this palette was measured against the
 * message pane only. Five of seven text tokens failed AA somewhere else —
 * --fg-mute read 4.37:1 on the pane but 3.24:1 on the rail, and the rail is
 * exactly where the sidebar's section labels live. globals.css has said
 * "checked on EVERY step a token can sit on" since the brand palette landed;
 * the check just was not mechanical, so skipping it cost nothing until now.
 */

const CSS = readFileSync(join(process.cwd(), "src/app/globals.css"), "utf8");

const TEXT = ["--fg", "--fg-soft", "--fg-mute", "--link", "--alert", "--live", "--brand"] as const;
const PLANES = [
  "--plane-pane",
  "--plane-sidebar",
  "--plane-raised",
  "--plane-selected",
  "--plane-rail",
] as const;

const AA_TEXT = 4.5;

function block(selector: string): string {
  const start = CSS.indexOf(`${selector} {`);
  expect(start, `missing block: ${selector}`).toBeGreaterThan(-1);
  return CSS.slice(start, CSS.indexOf("\n}", start));
}

/** Later blocks win, so resolve a state by layering focus over its base. */
function resolve(theme: "light" | "dark", accent: "full" | "focus"): Map<string, string> {
  const sources = [':root[data-chat-theme]'];
  if (theme === "dark") sources.push(':root[data-chat-theme="dark"]');
  if (accent === "focus") sources.push(`:root[data-chat-theme="${theme}"][data-chat-accent="focus"]`);

  const vars = new Map<string, string>();
  for (const selector of sources) {
    for (const [, name, value] of block(selector).matchAll(/(--[\w-]+):\s*(#[0-9A-Fa-f]{6})/g)) {
      vars.set(name, value);
    }
  }
  return vars;
}

function channel(byte: number): number {
  const c = byte / 255;
  return c <= 0.03928 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4;
}

function luminance(hex: string): number {
  const n = parseInt(hex.slice(1), 16);
  return (
    0.2126 * channel((n >> 16) & 0xff) +
    0.7152 * channel((n >> 8) & 0xff) +
    0.0722 * channel(n & 0xff)
  );
}

function contrast(a: string, b: string): number {
  const [x, y] = [luminance(a), luminance(b)];
  return (Math.max(x, y) + 0.05) / (Math.min(x, y) + 0.05);
}

const STATES = [
  ["light", "full"],
  ["light", "focus"],
  ["dark", "full"],
  ["dark", "focus"],
] as const;

describe("chat theme contrast", () => {
  it.each(STATES)("%s/%s clears AA for every text token on every plane", (theme, accent) => {
    const vars = resolve(theme, accent);
    const failures: string[] = [];

    for (const token of TEXT) {
      const fg = vars.get(token);
      expect(fg, `${token} unresolved in ${theme}/${accent}`).toBeDefined();
      for (const plane of PLANES) {
        const bg = vars.get(plane);
        expect(bg, `${plane} unresolved in ${theme}/${accent}`).toBeDefined();
        const ratio = contrast(fg!, bg!);
        if (ratio < AA_TEXT) {
          failures.push(`${token} (${fg}) on ${plane} (${bg}) = ${ratio.toFixed(2)}`);
        }
      }
    }

    expect(failures, failures.join("\n")).toEqual([]);
  });

  // Agent initials are text sitting on their identity tint, so they need the
  // same floor as anything else. --on-agent-tint flips per theme because the
  // light tints are dark and the dark tints are light.
  it.each(STATES)("%s/%s keeps agent initials legible on every tint", (theme, accent) => {
    const vars = resolve(theme, accent);
    const ink = vars.get("--on-agent-tint");
    expect(ink, "--on-agent-tint unresolved").toBeDefined();
    for (const n of [1, 2, 3]) {
      const tint = vars.get(`--agent-tint-${n}`);
      expect(tint, `--agent-tint-${n} unresolved`).toBeDefined();
      expect(
        contrast(ink!, tint!),
        `initials on --agent-tint-${n} in ${theme}/${accent}`,
      ).toBeGreaterThanOrEqual(AA_TEXT);
    }
  });

  it("checks every pair — a silent drop in coverage is the failure mode here", () => {
    expect(STATES.length * TEXT.length * PLANES.length).toBe(140);
  });

  // Primary and secondary copy carry real margin over the 4.5 floor rather
  // than squeaking past it. Pinned to the measured worst case so a future
  // palette edit that quietly erodes body-copy legibility fails here, even
  // though it would still technically pass AA.
  it.each([
    ["--fg", 10],
    ["--fg-soft", 5],
  ] as const)("keeps %s well clear of the floor, not merely passing", (token, floor) => {
    for (const [theme, accent] of STATES) {
      const vars = resolve(theme, accent);
      for (const plane of PLANES) {
        expect(
          contrast(vars.get(token)!, vars.get(plane)!),
          `${token} on ${plane} in ${theme}/${accent}`,
        ).toBeGreaterThanOrEqual(floor);
      }
    }
  });
});
