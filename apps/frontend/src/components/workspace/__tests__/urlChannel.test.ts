import { describe, it, expect } from "vitest";
import { MESSAGES_PANEL_ID } from "../panelRegistry";
import {
  parseCommand,
  commandSignature,
  stripOneShots,
  buildPanelUrl,
  clearPanelFromUrl,
  clearDurableParamFromUrl,
} from "../urlChannel";

describe("parseCommand", () => {
  it("parses a teams list command", () => {
    const c = parseCommand("?panel=teams-agents");
    expect(c?.descriptor.panelId).toBe("teams-agents");
    expect(c?.descriptor.params).toEqual({ teamsKey: "agents" });
    expect(c?.oneShots).toEqual({});
  });

  it("separates one-shots from durable params", () => {
    const c = parseCommand("?panel=teams-agents&new=task");
    expect(c?.descriptor.params).toEqual({ teamsKey: "agents" });
    expect(c?.oneShots).toEqual({ new: "task" });
  });

  it("resolves retired aliases", () => {
    expect(parseCommand("?panel=teams-approvals")?.descriptor.panelId).toBe("activity");
    // Settings took the Model panel: ?panel=credits names no dock panel.
    expect(parseCommand("?panel=credits")).toBeNull();
    // The org chart is a view of the roster now.
    const chartCmd = parseCommand("?panel=teams-org-chart");
    expect(chartCmd?.descriptor.panelId).toBe("teams-agents");
    expect(chartCmd?.descriptor.params).toEqual({ teamsKey: "agents" });
  });

  it("defines /chat?agent= as open-or-focus that agent's chat (deploy-success link)", () => {
    const c = parseCommand("?agent=agent-uuid-1");
    expect(c?.descriptor.panelId).toBe(MESSAGES_PANEL_ID);
    expect(c?.descriptor.params).toEqual({ agentId: "agent-uuid-1" });
    expect(c?.descriptor.component).toBe("chat");
  });

  it("defines /chat?room= as that room, with or without the panel key (meeting-notes mail link)", () => {
    const bare = parseCommand("?room=room-1");
    expect(bare?.descriptor.panelId).toBe(MESSAGES_PANEL_ID);
    expect(bare?.descriptor.params).toEqual({ roomId: "room-1" });
    expect(parseCommand("?room=room-1&message=m-2")?.descriptor.params).toEqual({ roomId: "room-1", messageId: "m-2" });
    expect(parseCommand("?panel=chat&room=room-1")?.descriptor.params).toEqual(bare?.descriptor.params);
  });

  it("returns null for bare /chat, billing returns, and unknown panels", () => {
    expect(parseCommand("")).toBeNull();
    expect(parseCommand("?checkout=success")).toBeNull();
    expect(parseCommand("?panel=definitely-not-a-panel")).toBeNull();
    expect(parseCommand("?panel=task-detail")).toBeNull(); // detail without id
  });

  // Ruling 3 (task-7a): the reconciler delivers chat params straight from
  // descriptor.params (WorkspaceShell.tsx's `openChatPanel(command.descriptor
  // .params.agentId)`), never via oneShots — a chat command never reaches the
  // oneShots-carrying openPanel() branch (workspaceShellReconciler.ts's
  // `panelKey = component === "chat" ? null : …`). So `message` is a durable
  // chat param, not a one-shot: nothing is stripped from the URL.
  it("treats ?panel=chat&room=…&message=… as a durable chat command", () => {
    const command = parseCommand("?panel=chat&room=r1&message=m1");
    expect(command?.descriptor.params).toMatchObject({ roomId: "r1", messageId: "m1" });
    expect(command?.oneShots).toEqual({});
    expect(stripOneShots("?panel=chat&room=r1&message=m1")).toBeNull();
  });
});

describe("stripOneShots", () => {
  it("removes exactly the one-shot params", () => {
    expect(stripOneShots("?panel=teams-artifacts&newIssue=onboarding&detailId=x")).toBe(
      "panel=teams-artifacts&detailId=x",
    );
  });
  it("returns null when nothing to strip", () => {
    expect(stripOneShots("?panel=teams-artifacts")).toBeNull();
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
      clearPanelFromUrl("?panel=task-detail&taskId=t-9", "task-detail"),
    ).toBe("");
  });
  it("clears ?agent when the closed panel is the bare-?agent chat command", () => {
    // Rule 7 makes bare ?agent a chat command, so closing that chat panel
    // must clear it — otherwise refresh resurrects the closed panel.
    expect(clearPanelFromUrl("?agent=agent-uuid-1", MESSAGES_PANEL_ID)).toBe("");
  });
  it("returns null when the closed panel is not the URL-named one", () => {
    expect(clearPanelFromUrl("?panel=teams-artifacts", "teams-issues")).toBeNull();
    expect(clearPanelFromUrl("?panel=task-detail&taskId=t-9", "activity")).toBeNull();
  });
  it("clears the explicit ?panel=chat grammar fully (agent OR agentId form)", () => {
    // Both forms are parseable grammar (describePanel accepts agent ?? agentId)
    // — closing the chat panel must leave no dangling ?panel=chat / ?agentId.
    expect(clearPanelFromUrl("?panel=chat&agent=a-1", MESSAGES_PANEL_ID)).toBe("");
    expect(clearPanelFromUrl("?panel=chat&agentId=a-1", MESSAGES_PANEL_ID)).toBe("");
  });
  it("clears ?path when the URL-named file viewer closes", () => {
    expect(clearPanelFromUrl("?panel=file-viewer&path=%2Fnotes.md", "file-viewer")).toBe("");
  });
  it("clears ?focus when the URL-named Activity closes", () => {
    expect(clearPanelFromUrl("?panel=activity&focus=paperclip%3Aapproval%3Aa1", "activity")).toBe("");
  });
});

describe("clearPanelFromUrl (Messages singleton)", () => {
  it("clears ?agent=<concrete default> when Messages closes", () => {
    expect(clearPanelFromUrl("?agent=friday-id", "chat:__default__")).toBe(
      "",
    );
  });
  it("clears a sentinel-named URL when Messages closes", () => {
    expect(
      clearPanelFromUrl("?agent=__default__", MESSAGES_PANEL_ID),
    ).toBe("");
  });
  it("clears an explicit chat URL without resolving the default agent", () => {
    expect(
      clearPanelFromUrl("?panel=chat&agentId=friday-id&checkout=success", MESSAGES_PANEL_ID),
    ).toBe("checkout=success");
  });
  it("clears Messages for a non-default agent too", () => {
    expect(
      clearPanelFromUrl("?agent=other-id", "chat:__default__"),
    ).toBe("");
  });
  it("passes plain (non-sentinel) closes straight through", () => {
    expect(clearPanelFromUrl("?agent=agent-1", MESSAGES_PANEL_ID)).toBe("");
    expect(clearPanelFromUrl("?panel=teams-agents", "teams-agents")).toBe(
      "",
    );
    expect(clearPanelFromUrl("?panel=teams-agents", "teams-issues")).toBeNull();
  });
});

describe("clearDurableParamFromUrl", () => {
  it("strips the param when the URL command names the panel", () => {
    expect(clearDurableParamFromUrl("?panel=activity&filter=mentions", "activity", "filter")).toBe(
      "panel=activity",
    );
  });
  it("returns null when the URL names a different panel", () => {
    expect(clearDurableParamFromUrl("?panel=teams-artifacts", "activity", "filter")).toBeNull();
    expect(clearDurableParamFromUrl("", "activity", "filter")).toBeNull();
  });
  it("returns null when the param is absent (no identical replaceState)", () => {
    expect(clearDurableParamFromUrl("?panel=activity", "activity", "filter")).toBeNull();
  });
});

describe("commandSignature", () => {
  it("ignores non-command params so billing strips don't retrigger the reconciler", () => {
    expect(commandSignature("?panel=teams-artifacts&checkout=success")).toBe(
      commandSignature("?panel=teams-artifacts"),
    );
  });
});

