import { describe, it, expect } from "vitest";
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

/**
 * Source-level guards for the "See her work →" deep link from the /chat agent
 * roster to the inline Issues panel filtered to that agent.
 *
 * Rendering ChatLayout / ChatPage end-to-end needs Clerk, SWR, gateway,
 * agents/billing hooks — far heavier than this regression deserves. Like the
 * sibling ChatLayout.teamsLink.test.ts, a source-level assertion is the most
 * direct way to lock in the wiring: the page exposes `openAgentIssues` setting
 * `panel`+`agent`, and ChatLayout renders the roster action gated on
 * `unifiedNav`.
 */

const pageSource = readFileSync(
  resolve(__dirname, "../../../app/chat/page.tsx"),
  "utf8",
);
const layoutSource = readFileSync(
  resolve(__dirname, "../ChatLayout.tsx"),
  "utf8",
);

describe("chat/page.tsx openAgentIssues", () => {
  it("defines an openAgentIssues callback", () => {
    expect(pageSource).toMatch(/openAgentIssues\s*=\s*useCallback/);
  });

  it("sets the teams-issues panel and the agent param", () => {
    expect(pageSource).toMatch(/params\.set\(\s*["']panel["']/);
    // The agent id is recorded so IssuesPanel can filter on it.
    expect(pageSource).toMatch(/params\.set\(\s*["']agent["']/);
    expect(pageSource).toMatch(/teams-issues|TEAMS_PANEL_PREFIX/);
  });

  it("passes openAgentIssues down to ChatLayout", () => {
    expect(pageSource).toMatch(/openAgentIssues=\{openAgentIssues\}/);
  });

  it("clears stale agent + detailId state on plain panel nav", () => {
    // setActivePanel (manual nav) must not carry a stale filter/detail.
    expect(pageSource).toMatch(/params\.delete\(\s*["']agent["']\s*\)/);
    expect(pageSource).toMatch(/params\.delete\(\s*["']detailId["']\s*\)/);
  });
});

describe("ChatLayout.tsx See-her-work roster action", () => {
  it("accepts an openAgentIssues prop", () => {
    expect(layoutSource).toMatch(/openAgentIssues\?\s*:\s*\(agentId:\s*string\)\s*=>\s*void/);
  });

  it("renders the See-her-work action unconditionally (no flag gate)", () => {
    expect(layoutSource).toMatch(/openAgentIssues/);
    expect(layoutSource).not.toMatch(/unifiedNav/);
  });

  it("hands openAgentIssues to the roster as its onOpenWork handler", () => {
    // The row markup (and the "See {name}'s work" label) moved into
    // AgentRoster, which is covered by render tests; the hand-off is what
    // ChatLayout still owns.
    expect(layoutSource).toMatch(/onOpenWork=\{\(id\) => openAgentIssues\?\.\(id\)\}/);
  });
});
