import { describe, it, expect } from "vitest";
import {
  resolvePanelAlias,
  chatPanelId,
  describePanel,
  durableParamsFor,
  clearablePanelParamKeys,
  DEFAULT_AGENT_SENTINEL,
  MESSAGES_PANEL_ID,
  settingsHrefFor,
  opensInPane,
} from "../panelRegistry";

describe("panelRegistry", () => {
  it("?panel=home opens Home (chats and projects); ?panel=activity keeps its filter and focus links", () => {
    expect(describePanel("home", { filter: "x" })).toEqual({ panelId: "home", component: "home", title: "Home", params: {} });
    expect(describePanel("activity", { filter: "approvals", focus: "a-1" })).toEqual({
      panelId: "activity", component: "activity", title: "Activity", params: { filter: "approvals", focus: "a-1" } });
  });
  it("teams-threads (the old Work › Threads overview) opens Activity", () => {
    expect(describePanel("teams-threads", {})).toMatchObject({ panelId: "activity", component: "activity", title: "Activity" });
  });

  it("task-detail is a pane panel that needs a taskId", () => {
    expect(describePanel("task-detail", { taskId: "t-1" })).toEqual({ panelId: "task-detail", component: "task", title: "Task", params: { taskId: "t-1" } });
    expect(describePanel("task-detail", {})).toBeNull();
    expect(durableParamsFor("task-detail")).toEqual(["panel", "taskId"]);
    expect(clearablePanelParamKeys("task-detail")).toEqual(["taskId"]);
  });

  it("resolves retired aliases (both routers' maps, merged)", () => {
    expect(resolvePanelAlias("teams-approvals")).toBe("activity");
    // The Inbox tab folded into Activity's For you.
    expect(resolvePanelAlias("teams-inbox")).toBe("activity");
    expect(resolvePanelAlias("teams-activity")).toBe("activity");
    // Home is no longer a destination: it and the keys it absorbed open Activity.
    expect(resolvePanelAlias("teams-dashboard")).toBe("activity");
    expect(resolvePanelAlias("teams-skills")).toBe("activity");
    expect(resolvePanelAlias("teams-costs")).toBe("activity");
    expect(describePanel("teams-dashboard", {})).toMatchObject({ panelId: "activity", component: "activity", title: "Activity" });
    expect(resolvePanelAlias("teams-artifacts")).toBe("activity");
    // Money is ONE page: its four absorbed keys alias to its panel id, or the
    // dock (which dedupes by panel id) opens four sibling tabs all called
    // "Money" — Home links three of them.
    for (const k of ["payments", "products", "customers", "spend"]) {
      expect(resolvePanelAlias(`teams-${k}`)).toBe("settings");
    }
    // The org chart is a VIEW of the roster, so its key aliases onto Agents.
    expect(resolvePanelAlias("teams-org-chart")).toBe("teams-agents");
    // The Paperclip board is gone: Support, Cases and every board page and detail open Activity.
    for (const k of ["support", "cases", "issues", "goals", "projects", "search", "issue-detail", "routine-detail", "project-detail",
      "goal-detail", "case-detail", "run-detail", "approval-detail"]) {
      expect(resolvePanelAlias(`teams-${k}`), k).toBe("activity");
    }
  });

  it("builds deterministic chat panel ids", () => {
    expect(chatPanelId("agent-123")).toBe("chat:agent-123");
    expect(chatPanelId(DEFAULT_AGENT_SENTINEL)).toBe("chat:__default__");
  });

  it("targets one Messages panel while carrying the requested agent", () => {
    expect(describePanel("chat", { agent: "agent-a" })?.panelId).toBe(MESSAGES_PANEL_ID);
    expect(describePanel("chat", { agent: "agent-b" })?.panelId).toBe(MESSAGES_PANEL_ID);
    expect(describePanel("chat", { agentId: "agent-b" })?.params).toEqual({ agentId: "agent-b" });
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

  it("keeps the agent detail control panel", () => {
    expect(describePanel("agents", { agent: "a-1" })).toEqual({
      panelId: "control:agents",
      component: "control",
      title: "Agents",
      params: { controlKey: "agents", agentId: "a-1" },
    });
  });

  // The one Settings page took these: none is a dock tab any more.
  it.each(["overview", "usage", "skills", "integrations", "connections", "llm", "credits", "teams-settings", "teams-members", "teams-accounts"])(
    "describes no dock panel for ?panel=%s",
    (key) => {
      expect(describePanel(key, {})).toBeNull();
    },
  );

  it.each([
    ["?panel=overview", "/settings"],
    ["?panel=usage", "/settings?panel=usage"],
    ["?panel=skills", "/settings"],
    ["?panel=integrations&from=app", "/settings?panel=connections&from=app"],
    ["?panel=connections&provider=notion", "/settings?panel=connections&provider=notion"],
    ["?panel=connections&connected=Notion", "/settings?panel=connections&connected=Notion"],
    ["?panel=llm&action=change-provider", "/settings?panel=billing&action=change-provider"],
    ["?panel=credits", "/settings?panel=billing"],
    ["?panel=teams-settings", "/settings?panel=workspace"],
    ["?panel=teams-members", "/settings?panel=members"],
    ["?panel=teams-accounts", "/settings?panel=connections"],
  ])("sends %s to %s", (search, href) => {
    expect(settingsHrefFor(search)).toBe(href);
  });

  it.each(["", "?panel=teams-issues", "?panel=agents&agent=a-1", "?panel=constructor", "?room=r1"])(
    "leaves %s on the shell",
    (search) => {
      expect(settingsHrefFor(search)).toBeNull();
    },
  );

  it("describes chat panels", () => {
    const d = describePanel("chat", { agent: "a-9" });
    expect(d).toEqual({
      panelId: MESSAGES_PANEL_ID,
      component: "chat",
      title: "Chat",
      params: { agentId: "a-9" },
    });
  });

  it("names durable params per panel id for clear-on-close", () => {
    expect(durableParamsFor("agent-inspector")).toEqual(["panel", "agent", "inspectorTab"]);
    // ?panel=file-viewer&path=… is registered grammar — close must clear the
    // path (and any agent binding), not just ?panel.
    expect(durableParamsFor("file-viewer")).toEqual(["panel", "path", "agent"]);
    // Bare ?agent names a chat panel (rule 7) — close must clear it, and the
    // explicit ?panel=chat grammar (agent OR agentId) must clear fully too.
    // room/message (permalinks, task-7a) are also chat-durable — close must
    // clear those too, or a refresh resurrects the closed room/message.
    expect(durableParamsFor("chat:a-1")).toEqual(["panel", "agent", "agentId", "room", "message", "session", "seq"]);
  });

  it("names the clearable PANEL params a plain re-open must reset", () => {
    expect(clearablePanelParamKeys("activity")).toEqual(["filter", "focus", "section"]);
    expect(clearablePanelParamKeys("file-viewer")).toEqual(["path", "agent"]);
    expect(clearablePanelParamKeys("agent-inspector")).toEqual(["agentId", "initialTab"]);
    expect(clearablePanelParamKeys("control:agents")).toEqual(["agentId", "initialTab"]);
    expect(clearablePanelParamKeys("chat:a-1")).toEqual([]);
    // roomId/messageId (permalinks) are clearable alongside agentId/
    // agentSelection: an agent open must clear a stale room target, and a
    // room open (openMessagePanel) must clear a stale agent target.
    expect(clearablePanelParamKeys(MESSAGES_PANEL_ID)).toEqual(["agentId", "agentSelection", "roomId", "messageId", "sessionId", "seq", "sessionIntent"]);
  });

  it("rejects prototype-chain keys — Object.prototype members are not panels", () => {
    expect(describePanel("constructor", {})).toBeNull();
    expect(describePanel("toString", {})).toBeNull();
    expect(describePanel("hasOwnProperty", {})).toBeNull();
    expect(describePanel("teams-constructor", { detailId: "x" })).toBeNull();
    expect(describePanel("teams-valueOf", {})).toBeNull();
    // resolvePanelAlias must not resolve inherited keys either.
    expect(resolvePanelAlias("constructor")).toBe("constructor");
  });

  it("file-viewer carries path + opening agent as durable params", () => {
    expect(describePanel("file-viewer", { path: "/a.md", agent: "a-1" })?.params).toEqual({
      path: "/a.md",
      agent: "a-1",
    });
    expect(describePanel("file-viewer", {})?.params).toEqual({});
  });

  it("describes the activity feed as a singleton carrying a filter and a focused item", () => {
    expect(describePanel("activity", {})).toEqual({
      panelId: "activity", component: "activity", title: "Activity", params: {},
    });
    expect(describePanel("activity", { focus: "paperclip:approval:ap-1", filter: "needs-you" })).toEqual({
      panelId: "activity", component: "activity", title: "Activity",
      params: { filter: "needs-you", focus: "paperclip:approval:ap-1" },
    });
    expect(durableParamsFor("activity")).toEqual(["panel", "filter", "focus", "section"]);
    expect(clearablePanelParamKeys("activity")).toEqual(["filter", "focus", "section"]);
  });

  it("describes Mail as a singleton carrying the open item", () => {
    expect(describePanel("mail", {})).toEqual({ panelId: "mail", component: "mail", title: "Mail", params: {} });
    expect(describePanel("mail", { item: "meeting:m1:1790553600" })).toEqual({
      panelId: "mail", component: "mail", title: "Mail", params: { item: "meeting:m1:1790553600" },
    });
    expect(durableParamsFor("mail")).toEqual(["panel", "item"]);
    expect(clearablePanelParamKeys("mail")).toEqual(["item"]);
  });

  it("aliases teams-org-chart onto the one Team panel", () => {
    expect(describePanel("teams-org-chart", {})).toEqual({ panelId: "teams-agents", component: "teams", title: "Team", params: { teamsKey: "agents" } });
    expect(describePanel("teams-agents", { view: "chart" })!.params).toEqual({ teamsKey: "agents" });
  });

  it("opens Activity for every old Inbox link: the Inbox tab, its Approvals and Activity keys", () => {
    for (const key of ["teams-inbox", "teams-approvals", "teams-activity"]) {
      expect(describePanel(key, { section: "needs-you" })).toEqual({ panelId: "activity", component: "activity", title: "Activity", params: {} });
    }
  });

  it("opens Activity for an old board link, its detail id dropped", () => {
    for (const key of ["teams-issues", "teams-goals", "teams-projects", "teams-issue-detail", "teams-approval-detail"]) {
      expect(describePanel(key, { detailId: "x", groupBy: "assignee" })).toEqual({ panelId: "activity", component: "activity", title: "Activity", params: {} });
    }
  });

  it("describePanel('session', …) retargets the Messages (chat) panel — there is no docked session panel", () => {
    expect(describePanel("session", { room: "r1", session: "s1" })).toEqual(
      describePanel("chat", { session: "s1" }),
    );
    // Sessions are workspace-scoped: a room riding along is ignored.
    expect(describePanel("chat", { room: "r1", session: "s1" })?.params).toEqual({ sessionId: "s1" });
    expect(describePanel("session", { room: "r1" })).toBeNull();
    expect(durableParamsFor(MESSAGES_PANEL_ID)).toContain("session");
  });

});

describe("Work dissolved: where its pages live now", () => {
  it("UX slice 4: Settings is a place with a section, one automation opens in the pane", () => {
    expect(describePanel("settings", { section: "billing" })).toEqual({ panelId: "settings", component: "settings", title: "Settings", params: { section: "billing" } });
    expect(describePanel("automation", {})).toBeNull();
    const automation = describePanel("automation", { id: "a1" })!;
    expect(automation).toEqual({ panelId: "automation", component: "automation", title: "Scheduled", params: { id: "a1" } });
    expect(opensInPane(automation)).toBe(true);
    expect(durableParamsFor("automation")).toEqual(["panel", "id"]);
    expect(describePanel("activity", { section: "scheduled" })?.params).toEqual({ section: "scheduled" });
  });

  it("UX slice 4: Work's Money and Automations open Settings › Business and Activity's Scheduled", () => {
    for (const key of ["teams-money", "teams-payments", "teams-customers", "teams-products", "teams-spend"]) {
      expect(describePanel(key, {})).toEqual({ panelId: "settings", component: "settings", title: "Settings", params: { section: "business" } });
    }
    expect(describePanel("teams-routines", {})).toEqual({ panelId: "activity", component: "activity", title: "Activity", params: { section: "scheduled" } });
  });

  it("voice pivot: Creations, Canvas, Library, Ideas and the Browser pane are gone; their old links open Activity", () => {
    for (const key of ["teams-artifacts", "canvas", "creations", "ideas", "browser"]) {
      expect(describePanel(key, { app: "a1", agent: "x" })).toEqual({ panelId: "activity", component: "activity", title: "Activity", params: {} });
    }
  });
});
