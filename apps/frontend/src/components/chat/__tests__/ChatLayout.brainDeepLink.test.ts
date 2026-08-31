import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

/**
 * Source-level guards for the two /chat mobile-shell seams F1/F5 found broken
 * in the final Brain-tab review:
 *   - F1: the desktop dock (TeamsDockPanel.tsx) forwards `path` from open
 *     params to the Brain panel via TeamsPanelRouter, but the mobile inline
 *     mount (this file) built its panelParams straight off the URL and never
 *     read `?path` at all — a `/chat?panel=teams-brain&path=…` deep link
 *     opened Brain with nothing selected on mobile.
 *   - F5(a): a raw `?panel=dreams` link never carries `&tab=dreams` on the
 *     wire — `describePanel`'s synthesis (panelRegistry.ts) only runs on the
 *     desktop URL channel — so the retired Dreams deep link landed on Notes
 *     on mobile instead of the Dreams tab.
 *
 * Rendering ChatPage's mobile shell end-to-end needs Clerk, SWR, gateway,
 * agents/billing hooks — far heavier than this regression deserves (see the
 * sibling ChatLayout.seeHerWork.test.ts) — so this pins the wiring at the
 * source level instead.
 */

const pageSource = readFileSync(resolve(__dirname, "../../../app/chat/page.tsx"), "utf8");

describe("chat/page.tsx mobile inline mount — Brain path + dreams-tab deep links", () => {
  it("reads ?path from the URL", () => {
    expect(pageSource).toMatch(/pathParam\s*=\s*searchParams\.get\(\s*["']path["']\s*\)/);
  });

  it("forwards path into the InlineTeamsPanel panelParams", () => {
    expect(pageSource).toMatch(/pathParam\s*\?\s*\{\s*path:\s*pathParam\s*\}\s*:\s*\{\}/);
  });

  it("synthesizes tab: \"dreams\" when the raw panel param is the retired dreams alias", () => {
    // Mirrors describePanel's `teamsKey === "brain" && panelKey === "dreams"`
    // synthesis (panelRegistry.ts) — checked against the RAW ?panel value,
    // not the alias-resolved one, so a fresh ?panel=teams-brain link is
    // untouched.
    expect(pageSource).toMatch(
      /tabParam\s*=\s*searchParams\.get\(\s*["']tab["']\s*\)\s*\?\?\s*\(\s*panelParam\s*===\s*["']dreams["']\s*\?\s*["']dreams["']\s*:\s*null\s*\)/,
    );
  });
});
