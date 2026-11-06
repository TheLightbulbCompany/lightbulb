import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { MAIN_MIN_WIDTH, PANE_MIN_WIDTH } from "../RightPane";
import { PANE_SHEET_QUERIES } from "../paneSheet";

// jsdom computes no layout, so this pins the CSS declaration itself.
describe(".workspace-shell viewport", () => {
  const css = readFileSync(resolve(process.cwd(), "src/components/workspace/workspace.css"), "utf8");
  const rule = css.slice(css.indexOf(".workspace-shell {"));
  const body = rule.slice(0, rule.indexOf("}"));

  it("is pinned to the viewport, not sized in vh", () => {
    // Owner report 2026-09-23: in the desktop app the whole shell rendered in
    // the top half of the window with blank space below. `height: 100vh` goes
    // stale in WKWebView after a window resize; `position: fixed; inset: 0`
    // always matches the real viewport and cannot be moved by document scroll.
    expect(body).toMatch(/position:\s*fixed/);
    expect(body).toMatch(/inset:\s*0/);
    expect(body).not.toMatch(/height:\s*100vh/);
  });

  it("turns the rail and sidebar into a drawer at phone widths", () => {
    // One shell at every width: without this block a 390 px phone gets a
    // 56 + 220 px chrome and a sliver of conversation.
    const phone = css.slice(css.indexOf("@media (max-width: 599px)"));
    expect(phone).toMatch(/\.workspace-shell > :is\(\.workspace-rail, \.cream-sidebar\) \{\s*display:\s*none/);
    expect(phone).toMatch(/\[data-nav-open="true"\] > :is\(\.workspace-rail, \.cream-sidebar\) \{ display: flex; \}/);
  });

  it("super-agent only: folds the conversations sidebar into a drawer below 1000 px, beside the rail (W8-2)", () => {
    const narrow = css.slice(css.indexOf("@media (min-width: 600px) and (max-width: 999px)"));
    expect(narrow).toMatch(/\.workspace-shell\[data-super-agent="true"\]\[data-sidebar="true"\] \{ grid-template-columns: 56px minmax\(0, 1fr\); grid-template-areas: "rail main"; \}/);
    expect(narrow).toMatch(/\[data-super-agent="true"\]\[data-sidebar="true"\]\[data-nav-open="true"\] > \.cream-sidebar \{ display: flex; \}/);
    // Without it, the 220 px sidebar column stays.
    expect(css).toMatch(/@media \(max-width: 1000px\) \{\s*\.workspace-shell:not\(\[data-super-agent="true"\]\) \{ grid-template-columns: 56px 220px minmax\(0, 1fr\); \}/);
  });

  it("without the super-agent, opens the pane as a sheet wherever the main view and the pane cannot both keep their minimums", () => {
    // jsdom computes no layout: derive the breakpoint from the minimums and that layout's chrome (rail + sidebar).
    const gate = ':not([data-super-agent="true"])';
    const fits = 56 + 220 + MAIN_MIN_WIDTH + 320;
    expect(PANE_SHEET_QUERIES.classic).toBe(`(max-width: ${fits - 1}px)`);
    const block = css.slice(css.indexOf(`@media (min-width: 600px) and (max-width: ${fits - 1}px)`));
    expect(block.startsWith("@media")).toBe(true);
    const sheet = block.slice(0, block.indexOf("}"));
    expect(sheet).toContain(`.workspace-shell${gate} .workspace-body:not([data-full]) > .workspace-pane {`);
    expect(sheet).toMatch(/position:\s*absolute/);
    expect(sheet).toMatch(/inset:\s*0/);
    expect(sheet).toMatch(/width:\s*auto !important/);
    // Full screen is excluded from the sheet, so it still sits beside the chat column.
    expect(block).toContain(`.workspace-shell${gate} .workspace-body[data-full] > .workspace-view--chat { flex: 0 1 360px; }`);
  });

  it("super-agent (UX slice 4): below 1000 px the pane is an overlay sheet from the right over a scrim, never squeezing the transcript", () => {
    expect(PANE_SHEET_QUERIES.superAgent).toBe("(max-width: 999px)");
    expect(PANE_MIN_WIDTH).toBe(360);
    const block = css.slice(css.indexOf('.workspace-shell[data-super-agent="true"] .workspace-pane[data-sheet] {'));
    const sheet = block.slice(0, block.indexOf("}"));
    expect(sheet).toMatch(/position:\s*absolute/);
    expect(sheet).toMatch(/inset:\s*0 0 0 auto/);
    expect(sheet).toMatch(/width:\s*min\(520px, 100% - 56px\)/);
    expect(css).toMatch(/\.workspace-pane-scrim \{[^}]*position:\s*absolute/);
  });
});

describe(".workspace-pane__body", () => {
  const css = readFileSync(resolve(process.cwd(), "src/components/workspace/workspace.css"), "utf8");
  const rule = (selector: string) => {
    const start = css.indexOf(`${selector} {`);
    return css.slice(start, css.indexOf("}", start));
  };

  it("is a bounded scroll container for every pane kind", () => {
    // Owner report 2026-10-15: the Ideas pane could not be scrolled with a mouse or trackpad.
    // jsdom computes no layout: pin the declarations that bound the body and make it scroll.
    const body = rule(".workspace-pane__body");
    expect(body).toMatch(/flex:\s*1/);
    expect(body).toMatch(/min-height:\s*0/);
    expect(body).toMatch(/overflow-y:\s*auto/);
    expect(rule(".workspace-pane")).toMatch(/min-height:\s*0/);
    expect(rule(".workspace-pane__body > *")).toMatch(/min-height:\s*0/);
  });
});
