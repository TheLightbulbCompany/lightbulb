import { describe, it, expect } from "vitest";
import {
  resolvePanelAlias,
  chatPanelId,
  describePanel,
  durableParamsFor,
  clearablePanelParamKeys,
  DEFAULT_AGENT_SENTINEL,
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
    // Dreams folded into Brain as a tab — the standalone control panel key
    // retires by alias, same as credits→llm.
    expect(resolvePanelAlias("dreams")).toBe("teams-brain");
    // Money is ONE page: its four absorbed keys alias to its panel id, or the
    // dock (which dedupes by panel id) opens four sibling tabs all called
    // "Money" — Home links three of them.
    for (const k of ["payments", "products", "customers", "spend"]) {
      expect(resolvePanelAlias(`teams-${k}`)).toBe("teams-money");
    }
  });

  it("describes a retired ?panel=dreams as the Brain panel, landing ON the Dreams tab (review finding 3)", () => {
    const d = describePanel("dreams", {});
    expect(d).toEqual({
      panelId: "teams-brain",
      component: "teams",
      title: "Brain",
      params: { teamsKey: "brain", tab: "dreams" },
    });
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
    expect(durableParamsFor("teams-issues")).toEqual(["panel", "agent", "groupBy"]);
    expect(durableParamsFor("teams-inbox")).toEqual(["panel", "section"]);
    // ?panel=file-viewer&path=… is registered grammar — close must clear the
    // path (and any agent binding), not just ?panel.
    expect(durableParamsFor("file-viewer")).toEqual(["panel", "path", "agent"]);
    // Bare ?agent names a chat panel (rule 7) — close must clear it, and the
    // explicit ?panel=chat grammar (agent OR agentId) must clear fully too.
    expect(durableParamsFor("chat:a-1")).toEqual(["panel", "agent", "agentId"]);
  });

  it("names the clearable PANEL params a plain re-open must reset", () => {
    expect(clearablePanelParamKeys("teams-issues")).toEqual(["agent", "groupBy"]);
    expect(clearablePanelParamKeys("teams-inbox")).toEqual(["section"]);
    expect(clearablePanelParamKeys("file-viewer")).toEqual(["path", "agent"]);
    expect(clearablePanelParamKeys("agent-inspector")).toEqual(["agentId", "initialTab"]);
    expect(clearablePanelParamKeys("control:agents")).toEqual(["agentId", "initialTab"]);
    expect(clearablePanelParamKeys("teams-dashboard")).toEqual([]);
    expect(clearablePanelParamKeys("chat:a-1")).toEqual([]);
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

  it("describes the brain panel with a durable ?path deep link", () => {
    expect(describePanel("teams-brain", {})).toEqual({
      panelId: "teams-brain",
      component: "teams",
      title: "Brain",
      params: { teamsKey: "brain" },
    });
    // ?path mirrors the file-viewer grammar: deep links select a page;
    // in-panel selection clicks never write it back.
    expect(describePanel("teams-brain", { path: "main/facts.md" })?.params).toEqual({
      teamsKey: "brain",
      path: "main/facts.md",
    });
    expect(durableParamsFor("teams-brain")).toEqual(["panel", "path"]);
    expect(clearablePanelParamKeys("teams-brain")).toEqual(["path"]);
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
    // way ?panel=dreams carries Brain's tab.
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

  it("aliases Goals and Projects onto Tasks, pre-grouped", () => {
    const goals = describePanel("teams-goals", {});
    expect(goals?.panelId).toBe("teams-issues");
    expect(goals?.title).toBe("Tasks");
    expect(goals?.params.groupBy).toBe("goal");

    const projects = describePanel("teams-projects", {});
    expect(projects?.panelId).toBe("teams-issues");
    expect(projects?.title).toBe("Tasks");
    expect(projects?.params.groupBy).toBe("project");
  });

  it("keeps one panel id when Goals, Projects and Tasks are all opened", () => {
    const tasks = describePanel("teams-issues", {})?.panelId;
    expect(describePanel("teams-goals", {})?.panelId).toBe(tasks);
    expect(describePanel("teams-projects", {})?.panelId).toBe(tasks);
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
      ["browser", "chat", "control", "file-viewer", "missing", "teams"].sort(),
    );
  });

  it("resolves the browser panel as a parameterless singleton", () => {
    // One container = one Xvfb display = one browser panel. A second open must
    // resolve to the SAME panelId so dockview focuses the existing panel
    // instead of mounting a second VNC session against the same framebuffer.
    const desc = describePanel("browser", {});
    expect(desc).toEqual({
      panelId: "browser",
      component: "browser",
      title: "Browser",
      params: {},
    });
    // Params the URL happens to carry must not leak into the descriptor —
    // they'd change the persisted layout blob for no behavioral reason.
    expect(describePanel("browser", { agent: "agent_x" })).toEqual(desc);
  });
});
