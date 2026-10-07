import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

/**
 * Source-level guard for the /chat mobile-shell seam F1 found broken in the
 * final Brain-tab review: the desktop dock (TeamsDockPanel.tsx) forwards
 * `path` from open params to the Brain panel via TeamsPanelRouter, but the
 * mobile inline mount (this file) built its panelParams straight off the URL
 * and never read `?path` at all — a `/chat?panel=teams-brain&path=…` deep link
 * opened Brain with nothing selected on mobile.
 *
 * (The sibling F5 guard — a raw `?panel=dreams` synthesizing Brain's Dreams
 * tab — retired with the tab: Dreams is its own control panel again.)
 *
 * Rendering ChatPage's mobile shell end-to-end needs Clerk, SWR, gateway,
 * agents/billing hooks — far heavier than this regression deserves (see the
 * sibling ChatLayout.seeHerWork.test.ts) — so this pins the wiring at the
 * source level instead.
 */

const pageSource = readFileSync(resolve(__dirname, "../../../app/chat/page.tsx"), "utf8");

describe("chat/page.tsx mobile inline mount — Brain path deep link", () => {
  it("reads ?path from the URL", () => {
    expect(pageSource).toMatch(/pathParam\s*=\s*searchParams\.get\(\s*["']path["']\s*\)/);
  });

  it("forwards path into the InlineTeamsPanel panelParams", () => {
    expect(pageSource).toMatch(/pathParam\s*\?\s*\{\s*path:\s*pathParam\s*\}\s*:\s*\{\}/);
  });
});
