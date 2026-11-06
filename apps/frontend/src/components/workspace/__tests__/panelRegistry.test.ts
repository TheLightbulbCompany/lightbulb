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
    expect(describePanel("canvas", { app: "app_1" })).toEqual({ panelId: "canvas", component: "canvas", title: "Canvas", params: { app: "app_1" } });
    expect(describePanel("teams-dashboard", {})).toMatchObject({ panelId: "activity", component: "activity", title: "Activity" });
    expect(resolvePanelAlias("teams-artifacts")).toBe("teams-artifacts");
    // Money is ONE page: its four absorbed keys alias to its panel id, or the
    // dock (which dedupes by panel id) opens four sibling tabs all called
    // "Money" — Home links three of them.
    for (const k of ["payments", "products", "customers", "spend"]) {
      expect(resolvePanelAlias(`teams-${k}`)).toBe("teams-money");
    }
    // The org chart is a VIEW of the roster, so its key aliases onto Agents.
    expect(resolvePanelAlias("teams-org-chart")).toBe("teams-agents");
    // The removed Support placeholder lands on Cases (links + saved layouts).
    expect(resolvePanelAlias("teams-support")).toBe("teams-cases");
    expect(describePanel("teams-support", {})?.params.teamsKey).toBe("cases");
    expect(describePanel("teams-case-detail", { detailId: "LB-C7" })?.panelId).toBe("teams-case-detail:LB-C7");
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

  it("describes teams list panels as singletons", () => {
    const d = describePanel("teams-artifacts", {});
    expect(d).toEqual({
      panelId: "teams-artifacts",
      component: "teams",
      title: "Library",
      params: { teamsKey: "artifacts" },
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
    ["?panel=skills", "/settings?panel=skills"],
    ["?panel=integrations&from=app", "/settings?panel=connections&from=app"],
    ["?panel=connections&provider=notion", "/settings?panel=connections&provider=notion"],
    ["?panel=connections&connected=Notion", "/settings?panel=connections&connected=Notion"],
    ["?panel=llm&action=change-provider", "/settings?panel=providers&action=change-provider"],
    ["?panel=credits", "/settings?panel=providers"],
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

  it("one label per panel — dock titles match the sidebar's IA labels", () => {
    // Clicking "Library" in the sidebar must not open a tab titled "Artifacts";
    expect(describePanel("teams-artifacts", {})?.title).toBe("Library");
  });

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
    expect(durableParamsFor("teams-issue-detail:ADM-1")).toEqual(["panel", "detailId"]);
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
    expect(clearablePanelParamKeys("activity")).toEqual(["filter", "focus"]);
    expect(clearablePanelParamKeys("file-viewer")).toEqual(["path", "agent"]);
    expect(clearablePanelParamKeys("agent-inspector")).toEqual(["agentId", "initialTab"]);
    expect(clearablePanelParamKeys("control:agents")).toEqual(["agentId", "initialTab"]);
    expect(clearablePanelParamKeys("browser")).toEqual([]);
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

  it("describes the browser panel as a paramless singleton", () => {
    // An owner has ONE AgentCore Browser profile, so a second open focuses the
    // existing panel instead of double-viewing the same live stream.
    expect(describePanel("browser", {})).toEqual({
      panelId: "browser",
      component: "browser",
      title: "Browser",
      params: {},
    });
    expect(describePanel("browser", { path: "/ignored" })?.params).toEqual({});
    expect(durableParamsFor("browser")).toEqual(["panel"]);
  });

  it("describes the activity feed as a singleton carrying a filter and a focused item", () => {
    expect(describePanel("activity", {})).toEqual({
      panelId: "activity", component: "activity", title: "Activity", params: {},
    });
    expect(describePanel("activity", { focus: "paperclip:approval:ap-1", filter: "needs-you" })).toEqual({
      panelId: "activity", component: "activity", title: "Activity",
      params: { filter: "needs-you", focus: "paperclip:approval:ap-1" },
    });
    expect(durableParamsFor("activity")).toEqual(["panel", "filter", "focus"]);
    expect(clearablePanelParamKeys("activity")).toEqual(["filter", "focus"]);
  });

  it("describes Mail as a singleton carrying the open item", () => {
    expect(describePanel("mail", {})).toEqual({ panelId: "mail", component: "mail", title: "Mail", params: {} });
    expect(describePanel("mail", { item: "meeting:m1:1790553600" })).toEqual({
      panelId: "mail", component: "mail", title: "Mail", params: { item: "meeting:m1:1790553600" },
    });
    expect(durableParamsFor("mail")).toEqual(["panel", "item"]);
    expect(clearablePanelParamKeys("mail")).toEqual(["item"]);
  });

  it("describes Ideas as a singleton that can put one agent first", () => {
    expect(describePanel("ideas", {})).toEqual({ panelId: "ideas", component: "ideas", title: "Ideas", params: {} });
    expect(describePanel("ideas", { agent: "a1" })?.params).toEqual({ agent: "a1" });
    expect(describePanel("ideas", { idea: "crm-setup" })?.params).toEqual({ idea: "crm-setup" });
    expect(durableParamsFor("ideas")).toEqual(["panel", "agent", "idea"]);
    expect(clearablePanelParamKeys("ideas")).toEqual(["agent", "idea"]);
  });

  it("describes the money panel with a durable ?day deep link", () => {
    expect(describePanel("teams-money", { day: "2026-09-05" })).toEqual({
      panelId: "teams-work", component: "teams", title: "Work", params: { teamsKey: "money", day: "2026-09-05" },
    });
    expect(describePanel("teams-money", {})!.params).toEqual({ teamsKey: "money" });
  });

  it("describes an absorbed Money key as ONE Money panel carrying its section", () => {
    // The whole point of the alias: same panelId for all five keys, so the
    // dock opens one tab — with the section synthesized off the RAW key, the
    // way ?panel=teams-org-chart carries the Agents chart view.
    expect(describePanel("teams-spend", {})).toEqual({
      panelId: "teams-work", component: "teams", title: "Work",
      params: { teamsKey: "money", section: "spend" },
    });
    for (const k of ["payments", "products", "customers"]) {
      expect(describePanel(`teams-${k}`, {})!.params).toEqual({ teamsKey: "money", section: k });
    }
    // A ?section= URL param is honoured on the canonical key, and only for a
    // real section — MoneyPanel feeds it into a `#money-${section}` selector.
    expect(describePanel("teams-money", { section: "customers" })!.params).toEqual({
      teamsKey: "money", section: "customers",
    });
    expect(describePanel("teams-money", { section: "constructor" })!.params).toEqual({ teamsKey: "money" });
    // The raw key wins: it is what the owner actually clicked.
    expect(describePanel("teams-spend", { section: "products" })!.params.section).toBe("spend");
  });

  it("aliases teams-org-chart onto ONE Agents panel, on the chart view", () => {
    // Same reason as Money's four keys: the dock dedupes by panel id, so a
    // router-level mapping would open a second tab also titled "Agents".
    expect(describePanel("teams-org-chart", {})).toEqual({
      panelId: "teams-agents",
      component: "teams",
      title: "Team",
      params: { teamsKey: "agents", view: "chart" },
    });
    // A plain Agents open carries no view — the panel's stored preference wins.
    expect(describePanel("teams-agents", {})!.params).toEqual({ teamsKey: "agents" });
    // ?view= is honoured on the canonical key, and only for a real view.
    expect(describePanel("teams-agents", { view: "chart" })!.params).toEqual({
      teamsKey: "agents",
      view: "chart",
    });
    expect(describePanel("teams-agents", { view: "constructor" })!.params).toEqual({
      teamsKey: "agents",
    });
    // The raw key wins: it is what the owner actually clicked.
    expect(describePanel("teams-org-chart", { view: "list" })!.params.view).toBe("chart");
  });

  it("opens Activity for every old Inbox link: the Inbox tab, its Approvals and Activity keys", () => {
    for (const key of ["teams-inbox", "teams-approvals", "teams-activity"]) {
      expect(describePanel(key, { section: "needs-you" })).toEqual({ panelId: "activity", component: "activity", title: "Activity", params: {} });
    }
  });

  it("opens Goals and Projects as their own pages inside the Work tab", () => {
    const goals = describePanel("teams-goals", {});
    expect(goals?.params).toEqual({ teamsKey: "goals" });
    const projects = describePanel("teams-projects", {});
    expect(projects?.params).toEqual({ teamsKey: "projects" });
  });

  it("carries an explicit ?groupBy, and ignores one that is not a grouping", () => {
    expect(describePanel("teams-issues", { groupBy: "assignee" })?.params.groupBy).toBe(
      "assignee",
    );
    expect(describePanel("teams-issues", { groupBy: "constructor" })?.params.groupBy)
      .toBeUndefined();
    expect(describePanel("teams-issues", { groupBy: "nonsense" })?.params.groupBy)
      .toBeUndefined();
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

describe("Work page: one dock tab for every task-board page", () => {
  it.each(["issues", "projects", "goals", "routines", "cases", "money"])("teams-%s opens in the Work tab", (key) => {
    const d = describePanel(`teams-${key}`, {})!;
    expect(d.panelId).toBe("teams-work");
    expect(d.title).toBe("Work");
    expect(d.params.teamsKey).toBe(key);
  });
  it("other Teams pages keep their own tab", () => {
    expect(describePanel("teams-artifacts", {})!.panelId).toBe("teams-artifacts");
    // Agents is its own place, Team.
    expect(describePanel("teams-agents", {})).toMatchObject({ panelId: "teams-agents", title: "Team" });
  });
  it("Work owns the union of its pages' durable params", () => {
    expect(durableParamsFor("teams-work")).toEqual(["panel", "agent", "groupBy", "view", "day", "section"]);
    expect(clearablePanelParamKeys("teams-work")).toEqual(["agent", "groupBy", "view", "day", "section"]);
  });
});

