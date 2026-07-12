import { describe, it, expect } from "vitest";
import {
  resolvePanelAlias,
  chatPanelId,
  describePanel,
  durableParamsFor,
  DEFAULT_AGENT_SENTINEL,
  PANEL_COMPONENT_KEYS,
} from "../panelRegistry";

describe("panelRegistry", () => {
  it("resolves retired aliases (both routers' maps, merged)", () => {
    expect(resolvePanelAlias("credits")).toBe("llm");
    expect(resolvePanelAlias("teams-approvals")).toBe("teams-inbox");
    expect(resolvePanelAlias("teams-members")).toBe("teams-settings");
    expect(resolvePanelAlias("teams-skills")).toBe("teams-dashboard");
    expect(resolvePanelAlias("teams-inbox")).toBe("teams-inbox");
  });

  it("builds deterministic chat panel ids", () => {
    expect(chatPanelId("agent-123")).toBe("chat:agent-123");
    expect(chatPanelId(DEFAULT_AGENT_SENTINEL)).toBe("chat:__default__");
  });

  it("describes teams list panels as singletons", () => {
    const d = describePanel("teams-inbox", {});
    expect(d).toEqual({
      panelId: "teams-inbox",
      component: "teams",
      title: "Inbox",
      params: { teamsKey: "inbox" },
    });
  });

  it("describes teams detail panels keyed per detailId", () => {
    const d = describePanel("teams-issue-detail", { detailId: "ADM-1" });
    expect(d?.panelId).toBe("teams-issue-detail:ADM-1");
    expect(d?.component).toBe("teams");
    expect(d?.params).toEqual({ teamsKey: "issue-detail", detailId: "ADM-1" });
  });

  it("returns null for a detail key without detailId", () => {
    expect(describePanel("teams-issue-detail", {})).toBeNull();
  });

  it("describes the agent inspector with durable params only", () => {
    const d = describePanel("agent-inspector", {
      agent: "a-1",
      inspectorTab: "channels",
      new: "task", // one-shot — must NOT survive into panel params
    });
    expect(d?.panelId).toBe("agent-inspector");
    expect(d?.component).toBe("control");
    expect(d?.params).toEqual({
      controlKey: "agent-inspector",
      agentId: "a-1",
      initialTab: "channels",
    });
  });

  it("describes legacy control keys (grammar parity for existing links)", () => {
    const d = describePanel("llm", {});
    expect(d).toEqual({
      panelId: "control:llm",
      component: "control",
      title: "Models & Providers",
      params: { controlKey: "llm" },
    });
  });

  it("describes chat panels with renderer always", () => {
    const d = describePanel("chat", { agent: "a-9" });
    expect(d).toEqual({
      panelId: "chat:a-9",
      component: "chat",
      title: "Chat",
      params: { agentId: "a-9" },
      renderer: "always",
    });
  });

  it("names durable params per panel id for clear-on-close", () => {
    expect(durableParamsFor("teams-issue-detail:ADM-1")).toEqual(["panel", "detailId"]);
    expect(durableParamsFor("agent-inspector")).toEqual(["panel", "agent", "inspectorTab"]);
    expect(durableParamsFor("teams-issues")).toEqual(["panel", "agent"]);
    expect(durableParamsFor("teams-inbox")).toEqual(["panel"]);
    // Bare ?agent names a chat panel (rule 7) — close must clear it.
    expect(durableParamsFor("chat:a-1")).toEqual(["agent"]);
  });

  it("registers exactly the component keys the shell's components map will have", () => {
    expect([...PANEL_COMPONENT_KEYS].sort()).toEqual(
      ["chat", "control", "file-viewer", "missing", "teams"].sort(),
    );
  });
});
