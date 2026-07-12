import { describe, it, expect } from "vitest";
import {
  parseCommand,
  commandSignature,
  stripOneShots,
  buildPanelUrl,
  clearPanelFromUrl,
} from "../urlChannel";

describe("parseCommand", () => {
  it("parses a teams list command", () => {
    const c = parseCommand("?panel=teams-issues&agent=a-1");
    expect(c?.descriptor.panelId).toBe("teams-issues");
    expect(c?.descriptor.params).toEqual({ teamsKey: "issues", agent: "a-1" });
    expect(c?.oneShots).toEqual({});
  });

  it("separates one-shots from durable params", () => {
    const c = parseCommand("?panel=teams-issues&new=task");
    expect(c?.descriptor.params).toEqual({ teamsKey: "issues" });
    expect(c?.oneShots).toEqual({ new: "task" });
  });

  it("resolves retired aliases", () => {
    expect(parseCommand("?panel=teams-approvals")?.descriptor.panelId).toBe("teams-inbox");
    expect(parseCommand("?panel=credits")?.descriptor.panelId).toBe("control:llm");
  });

  it("defines /chat?agent= as open-or-focus that agent's chat (deploy-success link)", () => {
    const c = parseCommand("?agent=agent-uuid-1");
    expect(c?.descriptor.panelId).toBe("chat:agent-uuid-1");
    expect(c?.descriptor.component).toBe("chat");
  });

  it("returns null for bare /chat, billing returns, and unknown panels", () => {
    expect(parseCommand("")).toBeNull();
    expect(parseCommand("?checkout=success")).toBeNull();
    expect(parseCommand("?panel=definitely-not-a-panel")).toBeNull();
    expect(parseCommand("?panel=teams-issue-detail")).toBeNull(); // detail without id
  });
});

describe("stripOneShots", () => {
  it("removes exactly the one-shot params", () => {
    expect(stripOneShots("?panel=teams-inbox&newIssue=onboarding&detailId=x")).toBe(
      "panel=teams-inbox&detailId=x",
    );
  });
  it("returns null when nothing to strip", () => {
    expect(stripOneShots("?panel=teams-inbox")).toBeNull();
  });
});

describe("buildPanelUrl", () => {
  it("builds canonical outbound URLs", () => {
    expect(buildPanelUrl("teams-issue-detail", { detailId: "ADM-9" })).toBe(
      "/chat?panel=teams-issue-detail&detailId=ADM-9",
    );
    expect(buildPanelUrl("agent-inspector", { agent: "a-1", inspectorTab: "channels" })).toBe(
      "/chat?panel=agent-inspector&agent=a-1&inspectorTab=channels",
    );
  });
});

describe("clearPanelFromUrl", () => {
  it("clears the full durable set when the closed panel is URL-named", () => {
    expect(
      clearPanelFromUrl("?panel=agent-inspector&agent=a-1&inspectorTab=channels", "agent-inspector", "agent-inspector"),
    ).toBe("");
    expect(
      clearPanelFromUrl("?panel=teams-issue-detail&detailId=ADM-9", "teams-issue-detail:ADM-9", "teams-issue-detail"),
    ).toBe("");
  });
  it("returns null when the closed panel is not the URL-named one", () => {
    expect(clearPanelFromUrl("?panel=teams-inbox", "teams-issues", "teams-inbox")).toBeNull();
    expect(
      clearPanelFromUrl("?panel=teams-issue-detail&detailId=OTHER", "teams-issue-detail:ADM-9", "teams-issue-detail"),
    ).toBeNull();
  });
});

describe("commandSignature", () => {
  it("ignores non-command params so billing strips don't retrigger the reconciler", () => {
    expect(commandSignature("?panel=teams-inbox&checkout=success")).toBe(
      commandSignature("?panel=teams-inbox"),
    );
  });
});
