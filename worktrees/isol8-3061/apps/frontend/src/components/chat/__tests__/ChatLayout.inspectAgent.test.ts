import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

/**
 * Source-level guards for the "Inspect agent" deep link from the /chat agent
 * roster to the per-agent inspector panel (increment 1 of the unified-nav
 * migration).
 *
 * Rendering ChatLayout / ChatPage end-to-end needs Clerk, SWR, gateway,
 * agents/billing hooks — far heavier than this regression deserves. Like the
 * sibling ChatLayout.seeHerWork.test.ts, a source-level assertion is the most
 * direct way to lock in the wiring: the page exposes `openAgentInspector`
 * setting `panel=agent-inspector`+`agent`, and ChatLayout renders the roster
 * action gated on `unifiedNav`.
 */

const pageSource = readFileSync(
  resolve(__dirname, "../../../app/chat/page.tsx"),
  "utf8",
);
const layoutSource = readFileSync(
  resolve(__dirname, "../ChatLayout.tsx"),
  "utf8",
);

describe("chat/page.tsx openAgentInspector", () => {
  it("defines an openAgentInspector callback", () => {
    expect(pageSource).toMatch(/openAgentInspector\s*=\s*useCallback/);
  });

  it("sets the agent-inspector panel and the agent param", () => {
    expect(pageSource).toMatch(/params\.set\(\s*["']panel["']\s*,\s*["']agent-inspector["']\s*\)/);
    expect(pageSource).toMatch(/params\.set\(\s*["']agent["']/);
  });

  it("passes openAgentInspector down to ChatLayout", () => {
    expect(pageSource).toMatch(/openAgentInspector=\{openAgentInspector\}/);
  });
});

describe("ChatLayout.tsx Inspect-agent roster action", () => {
  it("accepts an openAgentInspector prop", () => {
    expect(layoutSource).toMatch(/openAgentInspector\?\s*:\s*\(agentId:\s*string\)\s*=>\s*void/);
  });

  it("renders the Inspect action unconditionally (no flag gate)", () => {
    expect(layoutSource).toMatch(/openAgentInspector/);
    expect(layoutSource).not.toMatch(/unifiedNav/);
  });

  it("hands openAgentInspector to the roster as its onInspect handler", () => {
    // The row markup (and the `Inspect {name}` label) moved into AgentRoster,
    // which is covered by render tests. What ChatLayout still owns is this
    // hand-off; there is no `stopPropagation` to assert any more because the
    // action buttons are siblings of the row button, not nested inside it.
    expect(layoutSource).toMatch(/onInspect=\{\(id\) => openAgentInspector\?\.\(id\)\}/);
  });
});
