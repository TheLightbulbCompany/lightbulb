import { describe, it, expect } from "vitest";
import {
  resolvePanelAlias,
  chatPanelId,
  describePanel,
  durableParamsFor,
  clearablePanelParamKeys,
  DEFAULT_AGENT_SENTINEL,
  MESSAGES_PANEL_ID,
  PANEL_COMPONENT_KEYS,
} from "../panelRegistry";

describe("panelRegistry", () => {
  it("resolves retired aliases (both routers' maps, merged)", () => {
    expect(resolvePanelAlias("credits")).toBe("llm");
    expect(resolvePanelAlias("teams-approvals")).toBe("teams-inbox");
    expect(resolvePanelAlias("teams-members")).toBe("teams-settings");
    expect(resolvePanelAlias("teams-skills")).toBe("teams-dashboard");
    expect(resolvePanelAlias("teams-costs")).toBe("teams-dashboard");
    expect(resolvePanelAlias("teams-inbox")).toBe("teams-inbox");
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
    expect(describePanel("teams-support", {})?.panelId).toBe("teams-cases");
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
      title: "Model & Credits",
      params: { controlKey: "llm" },
    });
  });

  it("one label per panel — dock titles match the sidebar's IA labels", () => {
    // Clicking "Library" in the sidebar must not open a tab titled "Artifacts";
    // the llm panel converges on mobile's name, "Model & Credits".
    expect(describePanel("teams-dashboard", {})?.title).toBe("Home");
    expect(describePanel("teams-artifacts", {})?.title).toBe("Library");
    expect(describePanel("llm", {})?.title).toBe("Model & Credits");
  });

  it("describes chat panels with renderer always", () => {
    const d = describePanel("chat", { agent: "a-9" });
    expect(d).toEqual({
      panelId: MESSAGES_PANEL_ID,
      component: "chat",
      title: "Messages",
      params: { agentId: "a-9" },
      renderer: "always",
    });
  });

  it("names durable params per panel id for clear-on-close", () => {
    expect(durableParamsFor("teams-issue-detail:ADM-1")).toEqual(["panel", "detailId"]);
    expect(durableParamsFor("agent-inspector")).toEqual(["panel", "agent", "inspectorTab"]);
    expect(durableParamsFor("teams-issues")).toEqual(["panel", "agent", "groupBy"]);
    expect(durableParamsFor("teams-inbox")).toEqual(["panel", "section"]);
    // ?panel=file-viewer&path=… is registered grammar — close must clear the
    // path (and any agent binding), not just ?panel.
    expect(durableParamsFor("file-viewer")).toEqual(["panel", "path", "agent"]);
    // Bare ?agent names a chat panel (rule 7) — close must clear it, and the
    // explicit ?panel=chat grammar (agent OR agentId) must clear fully too.
    // room/message (permalinks, task-7a) are also chat-durable — close must
    // clear those too, or a refresh resurrects the closed room/message.
    expect(durableParamsFor("chat:a-1")).toEqual(["panel", "agent", "agentId", "room", "message", "session"]);
  });

  it("names the clearable PANEL params a plain re-open must reset", () => {
    expect(clearablePanelParamKeys("teams-issues")).toEqual(["agent", "groupBy"]);
    expect(clearablePanelParamKeys("teams-inbox")).toEqual(["section"]);
    expect(clearablePanelParamKeys("file-viewer")).toEqual(["path", "agent"]);
    expect(clearablePanelParamKeys("agent-inspector")).toEqual(["agentId", "initialTab"]);
    expect(clearablePanelParamKeys("control:agents")).toEqual(["agentId", "initialTab"]);
    expect(clearablePanelParamKeys("teams-dashboard")).toEqual([]);
    expect(clearablePanelParamKeys("chat:a-1")).toEqual([]);
    // roomId/messageId (permalinks) are clearable alongside agentId/
    // agentSelection: an agent open must clear a stale room target, and a
    // room open (openMessagePanel) must clear a stale agent target.
    expect(clearablePanelParamKeys(MESSAGES_PANEL_ID)).toEqual(["agentId", "agentSelection", "roomId", "messageId", "sessionId", "sessionIntent"]);
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
      // Stays in the document behind other tabs: DCV cannot start a display on a detached element.
      renderer: "always",
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

  it("describes the money panel with a durable ?day deep link", () => {
    expect(describePanel("teams-money", { day: "2026-09-05" })).toEqual({
      panelId: "teams-money", component: "teams", title: "Money", params: { teamsKey: "money", day: "2026-09-05" },
    });
    expect(describePanel("teams-money", {})!.params).toEqual({ teamsKey: "money" });
    expect(durableParamsFor("teams-money")).toEqual(["panel", "day", "section"]);
    expect(clearablePanelParamKeys("teams-money")).toEqual(["day", "section"]);
  });

  it("describes an absorbed Money key as ONE Money panel carrying its section", () => {
    // The whole point of the alias: same panelId for all five keys, so the
    // dock opens one tab — with the section synthesized off the RAW key, the
    // way ?panel=teams-org-chart carries the Agents chart view.
    expect(describePanel("teams-spend", {})).toEqual({
      panelId: "teams-money", component: "teams", title: "Money",
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
      title: "Agents",
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
    expect(durableParamsFor("teams-agents")).toEqual(["panel", "view"]);
    expect(clearablePanelParamKeys("teams-agents")).toEqual(["view"]);
  });

  it("aliases teams-activity onto Inbox, pre-scrolled to Recently", () => {
    const d = describePanel("teams-activity", {});
    expect(d?.panelId).toBe("teams-inbox");
    expect(d?.title).toBe("Inbox");
    expect(d?.params.section).toBe("recently");
  });

  it("keeps one panel id when Activity and Inbox are both opened", () => {
    expect(describePanel("teams-activity", {})?.panelId).toBe(
      describePanel("teams-inbox", {})?.panelId,
    );
  });

  it("carries an explicit ?section for a fresh Inbox deep link", () => {
    expect(describePanel("teams-inbox", { section: "happening-now" })?.params.section).toBe(
      "happening-now",
    );
  });

  it("ignores a section that is not a real Inbox band", () => {
    expect(describePanel("teams-inbox", { section: "constructor" })?.params.section).toBeUndefined();
  });

  it("lands a legacy ?tab=approvals bookmark on Needs you", () => {
    expect(describePanel("teams-inbox", { tab: "approvals" })?.params).toEqual({
      teamsKey: "inbox",
      section: "needs-you",
    });
  });

  it("resolves Goals and Projects as their own panels, not aliased onto Tasks", () => {
    const tasks = describePanel("teams-issues", {})?.panelId;

    const goals = describePanel("teams-goals", {});
    expect(goals?.panelId).toBe("teams-goals");
    expect(goals?.panelId).not.toBe(tasks);
    expect(goals?.title).toBe("Goals");
    expect(goals?.params).toEqual({ teamsKey: "goals" });

    const projects = describePanel("teams-projects", {});
    expect(projects?.panelId).toBe("teams-projects");
    expect(projects?.panelId).not.toBe(tasks);
    expect(projects?.title).toBe("Projects");
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

  it("registers exactly the component keys the shell's components map will have", () => {
    expect([...PANEL_COMPONENT_KEYS].sort()).toEqual(
      ["activity", "browser", "chat", "control", "file-viewer", "missing", "teams"].sort(),
    );
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
