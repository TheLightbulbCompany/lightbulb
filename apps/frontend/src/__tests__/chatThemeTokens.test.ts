import { readFileSync } from "node:fs";
import { join } from "node:path";
import { describe, expect, it } from "vitest";

/**
 * Guards the three-layer chat theme split (ladder / hue / semantic — see
 * globals.css and useChatTheme.ts).
 *
 * The architecture is only worth anything if it holds: the moment a semantic
 * colour starts varying with focus, "focus mode cannot change what a badge
 * means" stops being true, and nothing in review would catch it.
 */

const CSS = readFileSync(join(process.cwd(), "src/app/globals.css"), "utf8");

function block(selector: string): string {
  const start = CSS.indexOf(`${selector} {`);
  expect(start, `missing block: ${selector}`).toBeGreaterThan(-1);
  const end = CSS.indexOf("\n}", start);
  return CSS.slice(start, end);
}

const LIGHT = ':root[data-chat-theme]';
const DARK = ':root[data-chat-theme="dark"]';
const LIGHT_FOCUS = ':root[data-chat-theme="light"][data-chat-accent="focus"]';
const DARK_FOCUS = ':root[data-chat-theme="dark"][data-chat-accent="focus"]';

const LADDER = [
  "--plane-rail",
  "--plane-sidebar",
  "--plane-pane",
  "--plane-raised",
  "--plane-edge",
];
const TYPE = ["--fg", "--fg-soft", "--fg-mute"];
const HUE = ["--brand", "--agent-tint-1", "--agent-tint-2", "--agent-tint-3", "--on-agent-tint"];
const SEMANTIC = ["--link", "--alert", "--live"];

describe("chat theme tokens", () => {
  it.each([LIGHT, DARK, LIGHT_FOCUS, DARK_FOCUS])(
    "%s defines the whole ladder",
    (selector) => {
      const body = block(selector);
      for (const token of LADDER) expect(body, token).toContain(`${token}:`);
    },
  );

  it.each([LIGHT, DARK, LIGHT_FOCUS, DARK_FOCUS])(
    "%s defines the type scale and the hue layer",
    (selector) => {
      const body = block(selector);
      for (const token of [...TYPE, ...HUE]) expect(body, token).toContain(`${token}:`);
    },
  );

  it.each([LIGHT, DARK])("%s defines the semantic set", (selector) => {
    const body = block(selector);
    for (const token of SEMANTIC) expect(body, token).toContain(`${token}:`);
  });

  // The load-bearing assertion. Focus drains the HUE layer and nothing else;
  // if a semantic token ever appears in a focus block, unread/alert/live have
  // become theme-dependent and the guarantee is gone.
  it.each([LIGHT_FOCUS, DARK_FOCUS])("%s never redefines a semantic colour", (selector) => {
    const body = block(selector);
    for (const token of SEMANTIC) expect(body, token).not.toContain(`${token}:`);
  });

  it("keeps the light ladder separated by more than the 2% it replaced", () => {
    // The bug this whole change exists to fix: .workspace-rail and
    // .room-directory both painted --cream-deep, so rail and sidebar were one
    // surface 2% from the pane. Assert real daylight between all three.
    const body = block(LIGHT);
    const read = (token: string) => {
      const m = body.match(new RegExp(`${token}:\\s*(#[0-9A-Fa-f]{6})`));
      expect(m, token).not.toBeNull();
      return parseInt(m![1].slice(1, 3), 16); // red channel stands in for level
    };
    const rail = read("--plane-rail");
    const sidebar = read("--plane-sidebar");
    const pane = read("--plane-pane");

    expect(rail).toBeLessThan(sidebar);
    expect(sidebar).toBeLessThan(pane);
    expect(pane - rail).toBeGreaterThanOrEqual(24); // ~10%, vs the old ~5/255
  });

  it("puts the reading surface on the lightest plane in dark too", () => {
    const body = block(DARK);
    const read = (token: string) => {
      const m = body.match(new RegExp(`${token}:\\s*(#[0-9A-Fa-f]{6})`));
      return parseInt(m![1].slice(1, 3), 16);
    };
    expect(read("--plane-rail")).toBeLessThan(read("--plane-sidebar"));
    expect(read("--plane-sidebar")).toBeLessThan(read("--plane-pane"));
  });
});
