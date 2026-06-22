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

  it("gates the Inspect action on unifiedNav", () => {
    // The button must only render when unifiedNav is truthy — off-flag the
    // agent-inspector panel wouldn't route.
    expect(layoutSource).toMatch(/unifiedNav\s*\?[\s\S]*?openAgentInspector/);
  });

  it("calls openAgentInspector with the agent id and stops row-click propagation", () => {
    expect(layoutSource).toMatch(/openAgentInspector\?\.\(agent\.id\)/);
    expect(layoutSource).toMatch(/e\.stopPropagation\(\);\s*openAgentInspector/);
  });

  it("labels the action Inspect with the agent name", () => {
    expect(layoutSource).toMatch(/Inspect \$\{agentDisplayName\(agent\)\}/);
  });
});
