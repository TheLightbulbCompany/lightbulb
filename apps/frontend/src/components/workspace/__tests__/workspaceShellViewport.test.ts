import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";

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
});
