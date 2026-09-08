import { describe, it, expect } from "vitest";
import {
  parseCommand,
  commandSignature,
  stripOneShots,
  buildPanelUrl,
  clearPanelFromUrl,
  clearClosedPanelFromUrl,
  clearDurableParamFromUrl,
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
    // Dreams is its own control panel again — ?panel=dreams opens it, not the
    // Brain panel it spent a while folded into.
    const dreamsCmd = parseCommand("?panel=dreams");
    expect(dreamsCmd?.descriptor.panelId).toBe("control:dreams");
    expect(dreamsCmd?.descriptor.params).toEqual({ controlKey: "dreams" });
    // The org chart is a view of the roster now.
    const chartCmd = parseCommand("?panel=teams-org-chart");
    expect(chartCmd?.descriptor.panelId).toBe("teams-agents");
    expect(chartCmd?.descriptor.params).toEqual({ teamsKey: "agents", view: "chart" });
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
      clearPanelFromUrl("?panel=agent-inspector&agent=a-1&inspectorTab=channels", "agent-inspector"),
    ).toBe("");
    expect(
      clearPanelFromUrl("?panel=teams-issue-detail&detailId=ADM-9", "teams-issue-detail:ADM-9"),
    ).toBe("");
  });
  it("clears ?agent when the closed panel is the bare-?agent chat command", () => {
    // Rule 7 makes bare ?agent a chat command, so closing that chat panel
    // must clear it — otherwise refresh resurrects the closed panel.
    expect(clearPanelFromUrl("?agent=agent-uuid-1", "chat:agent-uuid-1")).toBe("");
  });
  it("returns null when the closed panel is not the URL-named one", () => {
    expect(clearPanelFromUrl("?panel=teams-inbox", "teams-issues")).toBeNull();
    expect(
      clearPanelFromUrl("?panel=teams-issue-detail&detailId=OTHER", "teams-issue-detail:ADM-9"),
    ).toBeNull();
  });
  it("clears the explicit ?panel=chat grammar fully (agent OR agentId form)", () => {
    // Both forms are parseable grammar (describePanel accepts agent ?? agentId)
    // — closing the chat panel must leave no dangling ?panel=chat / ?agentId.
    expect(clearPanelFromUrl("?panel=chat&agent=a-1", "chat:a-1")).toBe("");
    expect(clearPanelFromUrl("?panel=chat&agentId=a-1", "chat:a-1")).toBe("");
  });
  it("clears ?path when the URL-named file viewer closes", () => {
    expect(clearPanelFromUrl("?panel=file-viewer&path=%2Fnotes.md", "file-viewer")).toBe("");
  });
  it("clears ?section when the URL-named inbox closes", () => {
    expect(clearPanelFromUrl("?panel=teams-inbox&section=needs-you", "teams-inbox")).toBe("");
  });
});

describe("clearClosedPanelFromUrl (sentinel unification)", () => {
  it("clears ?agent=<concrete default> when the sentinel chat panel closes", () => {
    // The reconciler focused the sentinel for this command (openChatPanel's
    // unification), so the close event carries chat:__default__ while the URL
    // names the concrete id.
    expect(clearClosedPanelFromUrl("?agent=friday-id", "chat:__default__", () => "friday-id")).toBe(
      "",
    );
  });
  it("clears a sentinel-named URL when the concrete default panel closes", () => {
    expect(
      clearClosedPanelFromUrl("?agent=__default__", "chat:friday-id", () => "friday-id"),
    ).toBe("");
  });
  it("returns null when the resolver doesn't know the default agent", () => {
    expect(
      clearClosedPanelFromUrl("?agent=friday-id", "chat:__default__", () => undefined),
    ).toBeNull();
  });
  it("does not unify a NON-default concrete id with the sentinel", () => {
    expect(
      clearClosedPanelFromUrl("?agent=other-id", "chat:__default__", () => "friday-id"),
    ).toBeNull();
  });
  it("passes plain (non-sentinel) closes straight through", () => {
    expect(clearClosedPanelFromUrl("?agent=agent-1", "chat:agent-1", () => undefined)).toBe("");
    expect(clearClosedPanelFromUrl("?panel=teams-inbox", "teams-inbox", () => "friday-id")).toBe(
      "",
    );
    expect(clearClosedPanelFromUrl("?panel=teams-inbox", "teams-issues", () => "friday-id")).toBeNull();
  });
});

describe("clearDurableParamFromUrl", () => {
  it("strips the param when the URL command names the panel", () => {
    expect(clearDurableParamFromUrl("?panel=teams-issues&agent=a-1", "teams-issues", "agent")).toBe(
      "panel=teams-issues",
    );
  });
  it("returns null when the URL names a different panel", () => {
    expect(clearDurableParamFromUrl("?panel=teams-inbox", "teams-issues", "agent")).toBeNull();
    expect(clearDurableParamFromUrl("", "teams-issues", "agent")).toBeNull();
  });
  it("returns null when the param is absent (no identical replaceState)", () => {
    expect(clearDurableParamFromUrl("?panel=teams-issues", "teams-issues", "agent")).toBeNull();
  });
});

describe("commandSignature", () => {
  it("ignores non-command params so billing strips don't retrigger the reconciler", () => {
    expect(commandSignature("?panel=teams-inbox&checkout=success")).toBe(
      commandSignature("?panel=teams-inbox"),
    );
  });
});
