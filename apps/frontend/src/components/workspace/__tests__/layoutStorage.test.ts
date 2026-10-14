import { describe, it, expect, beforeEach, afterAll } from "vitest";
import { installStoragePolyfill } from "@/test-utils/storagePolyfill";
import type { SerializedDockview } from "dockview-react";
import { DEFAULT_AGENT_SENTINEL, MESSAGES_PANEL_ID } from "../panelRegistry";
import {
  layoutStorageKey,
  readLayout,
  writeLayout,
  clearLayout,
  sanitizeLayout,
} from "../layoutStorage";

const restore = installStoragePolyfill();
afterAll(restore);
beforeEach(() => window.localStorage.clear());

function minimalLayout(panels: SerializedDockview["panels"]): SerializedDockview {
  return {
    grid: {
      root: { type: "branch", data: [] },
      height: 600,
      width: 800,
      orientation: "HORIZONTAL" as SerializedDockview["grid"]["orientation"],
    },
    panels,
  };
}

describe("layoutStorage", () => {
  it("scopes the key per owner", () => {
    expect(layoutStorageKey("org_1")).toBe("isol8.workspace.layout.v3:org_1");
  });

  it("round-trips a valid layout", () => {
    const layout = minimalLayout({
      "teams-inbox": { id: "teams-inbox", contentComponent: "teams", params: { teamsKey: "inbox" } },
    });
    writeLayout("u1", layout);
    expect(readLayout("u1")).toEqual(layout);
  });

  it("drops every superseded blob on read, leaving the current one intact", () => {
    const layout = minimalLayout({ "file-viewer": { id: "file-viewer", contentComponent: "file-viewer", params: {} } });
    writeLayout("u1", layout);
    window.localStorage.setItem("isol8.workspace.layout.v1:u1", "{}");
    window.localStorage.setItem("isol8.workspace.layout.v2:u1", "{}");
    expect(readLayout("u1")).toEqual(layout);
    // v1 is cleared too, not just the immediately-previous version — otherwise
    // it is stranded forever in the storage of anyone who last loaded pre-v2.
    expect(window.localStorage.getItem("isol8.workspace.layout.v1:u1")).toBeNull();
    expect(window.localStorage.getItem("isol8.workspace.layout.v2:u1")).toBeNull();
  });

  it("refuses a v2 blob, so a saved SPLIT layout cannot survive into tabs-only", () => {
    // The whole point of the v3 bump: a v2 payload is a persisted three-pane
    // split. Restoring it would put an existing owner back in the tiled shell
    // and hide the change from exactly the people who already use the product.
    window.localStorage.setItem(
      layoutStorageKey("u1"),
      JSON.stringify({ version: 2, layout: minimalLayout({}) }),
    );
    expect(readLayout("u1")).toBeNull();
  });

  it("returns null on version mismatch or garbage", () => {
    window.localStorage.setItem(layoutStorageKey("u1"), JSON.stringify({ version: 1, layout: {} }));
    expect(readLayout("u1")).toBeNull();
    window.localStorage.setItem(layoutStorageKey("u1"), "not-json{");
    expect(readLayout("u1")).toBeNull();
  });

  it("sanitizer remaps unknown contentComponent to 'missing' (fromJSON throws otherwise)", () => {
    const dirty = minimalLayout({
      ghost: { id: "ghost", contentComponent: "deleted-panel-type", params: {} },
    });
    const clean = sanitizeLayout(dirty);
    expect(clean.panels.ghost.contentComponent).toBe("missing");
  });

  it("sanitizer strips floating/popout/edge groups and one-shot params", () => {
    const dirty = {
      ...minimalLayout({
        "teams-issues": {
          id: "teams-issues",
          contentComponent: "teams",
          params: { teamsKey: "issues", new: "task", agent: "a-1" },
        },
      }),
      floatingGroups: [{}],
      popoutGroups: [{}],
      edgeGroups: {},
    } as unknown as SerializedDockview;
    const clean = sanitizeLayout(dirty);
    expect(clean.floatingGroups).toBeUndefined();
    expect(clean.popoutGroups).toBeUndefined();
    expect((clean as unknown as Record<string, unknown>).edgeGroups).toBeUndefined();
    expect(clean.panels["teams-work"].params).toEqual({ teamsKey: "issues", agent: "a-1" });
  });

  it("sanitizer folds a saved task-board tab into the one Work tab", () => {
    const dirty = minimalLayout({
      "teams-projects": { id: "teams-projects", contentComponent: "teams", title: "Projects", params: { teamsKey: "projects" } },
      "teams-payments": { id: "teams-payments", contentComponent: "teams", title: "Payments", params: { teamsKey: "payments" } },
    });
    const clean = sanitizeLayout(dirty);
    expect(Object.keys(clean.panels)).toContain("teams-work");
    expect(clean.panels["teams-work"]).toMatchObject({ title: "Work", params: { teamsKey: "projects" } });
    // A second saved Work page cannot share the id; it is dropped, not merged.
    expect(clean.panels["teams-projects"]).toBeUndefined();
    expect(clean.panels["teams-payments"]).toBeUndefined();
  });

  it("sanitizer heals a stale serialized tab title from the registry", () => {
    const dirty = minimalLayout({
      "teams-artifacts": {
        id: "teams-artifacts",
        contentComponent: "teams",
        title: "Artifacts", // saved before the Library rename
        params: { teamsKey: "artifacts" },
      },
    });
    const clean = sanitizeLayout(dirty);
    expect(clean.panels["teams-artifacts"].title).toBe("Library");
  });

  it("sanitizer drops a persisted session dock panel entirely, pruning its now-empty leaf, instead of leaving a dead 'missing' tab", () => {
    // Unlike an ordinary unrecognized contentComponent (the "ghost" case
    // above), a docked session panel is a KNOWN retired shape: remapping it to
    // 'missing' would just strand a dead tab in the restored layout forever.
    // It is dropped the same way a retired-panel duplicate is dropped, so an
    // empty leaf left behind gets pruned along with it.
    const dirty: SerializedDockview = {
      grid: {
        root: {
          type: "branch",
          data: [
            { type: "leaf", data: { id: "group-session", views: ["session:s1"], activeView: "session:s1" } },
            { type: "leaf", data: { id: "group-messages", views: [MESSAGES_PANEL_ID], activeView: MESSAGES_PANEL_ID } },
          ],
        },
        height: 600,
        width: 800,
        orientation: "HORIZONTAL" as SerializedDockview["grid"]["orientation"],
      },
      panels: {
        "session:s1": { id: "session:s1", contentComponent: "session", title: "stale", params: { roomId: "r", sessionId: "s" } },
        [MESSAGES_PANEL_ID]: { id: MESSAGES_PANEL_ID, contentComponent: "chat", params: {} },
      },
    };
    const clean = sanitizeLayout(dirty);
    expect(clean.panels["session:s1"]).toBeUndefined();
    expect(clean.panels[MESSAGES_PANEL_ID]).toBeDefined();
    // The leaf that held only the retired session panel is pruned, not kept
    // empty — the surviving sibling is promoted to the root.
    const root = clean.grid.root as unknown as { type: string; data: { id?: string } };
    expect(root.type).toBe("leaf");
    expect((root.data as { id: string }).id).toBe("group-messages");

    writeLayout("u-session", dirty);
    expect(readLayout("u-session")).toEqual(clean);
  });

  // Brain and the dreams/sessions/channels control panels lost their surface
  // with the OpenClaw runtime — leaving them to the generic path restores an
  // "Unknown panel: brain" or three tabs silently rendering Overview. They are
  // DROPPED, like session:. `browser` is NOT in that set: #2779 dropped the
  // container VNC panel, and the AgentCore live view took the same id back, so
  // a saved layout holding one must RESTORE it (asserted below).
  it("drops every panel whose surface was deleted with the OpenClaw runtime", () => {
    const retired = ["teams-brain", "control:dreams", "control:sessions", "control:channels"];
    const dirty: SerializedDockview = {
      grid: {
        root: {
          type: "branch",
          data: [
            { type: "leaf", data: { id: "group-retired", views: retired, activeView: "teams-brain" } },
            { type: "leaf", data: { id: "group-messages", views: [MESSAGES_PANEL_ID], activeView: MESSAGES_PANEL_ID } },
          ],
        },
        height: 600,
        width: 800,
        orientation: "HORIZONTAL" as SerializedDockview["grid"]["orientation"],
      },
      panels: {
        "teams-brain": { id: "teams-brain", contentComponent: "teams", title: "Brain", params: { teamsKey: "brain" } },
        "control:dreams": { id: "control:dreams", contentComponent: "control", title: "Dreams", params: { controlKey: "dreams" } },
        "control:sessions": { id: "control:sessions", contentComponent: "control", title: "Sessions", params: { controlKey: "sessions" } },
        "control:channels": { id: "control:channels", contentComponent: "control", title: "Channels", params: { controlKey: "channels" } },
        [MESSAGES_PANEL_ID]: { id: MESSAGES_PANEL_ID, contentComponent: "chat", params: {} },
      },
    };

    const clean = sanitizeLayout(dirty);
    for (const id of retired) expect(clean.panels[id]).toBeUndefined();
    expect(clean.panels[MESSAGES_PANEL_ID]).toBeDefined();
    // Nothing is remapped to the missing-panel placeholder on the way out.
    expect(
      Object.values(clean.panels).some((p) => p.contentComponent === "missing"),
    ).toBe(false);
    // The leaf that held only retired panels is pruned, not kept empty.
    const root = clean.grid.root as unknown as { type: string; data: { id?: string } };
    expect(root.type).toBe("leaf");
    expect((root.data as { id: string }).id).toBe("group-messages");

    writeLayout("u-retired", dirty);
    expect(readLayout("u-retired")).toEqual(clean);
  });

  // The regression the line above guards against: `browser` was in
  // RETIRED_PANEL_IDS while the panel did not exist. Re-adding the panel kind
  // without removing it there would silently delete the AgentCore live view
  // from every restored layout.
  it("restores a persisted browser panel, with its title re-derived", () => {
    const dirty = minimalLayout({
      browser: { id: "browser", contentComponent: "browser", title: "Stale title", params: {} },
    });
    const clean = sanitizeLayout(dirty);
    expect(clean.panels.browser?.contentComponent).toBe("browser");
    expect(clean.panels.browser?.title).toBe("Browser");
  });

  it("restores a persisted Mail panel, with its title re-derived", () => {
    const dirty = minimalLayout({
      mail: { id: "mail", contentComponent: "mail", title: "Inbox", params: { item: "mail:k1" } },
    });
    const clean = sanitizeLayout(dirty);
    expect(clean.panels.mail?.contentComponent).toBe("mail");
    expect(clean.panels.mail?.title).toBe("Mail");
  });

  it("sanitizer strips persist-only params (initialTab) so a nav intent can't restore forever", () => {
    const dirty = minimalLayout({
      "agent-inspector": {
        id: "agent-inspector",
        contentComponent: "control",
        params: { controlKey: "agent-inspector", agentId: "a", initialTab: "channels" },
      },
    });
    const clean = sanitizeLayout(dirty);
    expect(clean.panels["agent-inspector"].params).toEqual({
      controlKey: "agent-inspector",
      agentId: "a",
    });
  });

  function leafLayout(views: string[], activeView: string, panels: SerializedDockview["panels"]): SerializedDockview {
    return {
      grid: {
        root: {
          type: "leaf",
          data: { id: "group-1", views, activeView },
        },
        height: 600,
        width: 800,
        orientation: "HORIZONTAL" as SerializedDockview["grid"]["orientation"],
      },
      panels,
    } as unknown as SerializedDockview;
  }

  it("sanitizer migrates a retired teams-costs layout entry to teams-dashboard", () => {
    const dirty = leafLayout(["teams-costs"], "teams-costs", {
      "teams-costs": {
        id: "teams-costs",
        contentComponent: "teams",
        params: { teamsKey: "costs" },
      },
    });
    const clean = sanitizeLayout(dirty);
    expect(clean.panels["teams-costs"]).toBeUndefined();
    expect(clean.panels["teams-dashboard"]).toEqual({
      id: "teams-dashboard",
      contentComponent: "teams",
      params: { teamsKey: "dashboard" },
    });
    const leafData = (clean.grid.root as unknown as { data: { views: string[]; activeView: string } }).data;
    expect(leafData.views).toEqual(["teams-dashboard"]);
    expect(leafData.activeView).toBe("teams-dashboard");
  });

  it("sanitizer migrates a retired teams-skills layout entry to teams-dashboard", () => {
    const dirty = leafLayout(["teams-skills"], "teams-skills", {
      "teams-skills": {
        id: "teams-skills",
        contentComponent: "teams",
        params: { teamsKey: "skills" },
      },
    });
    const clean = sanitizeLayout(dirty);
    expect(clean.panels["teams-skills"]).toBeUndefined();
    expect(clean.panels["teams-dashboard"]).toEqual({
      id: "teams-dashboard",
      contentComponent: "teams",
      params: { teamsKey: "dashboard" },
    });
    const leafData = (clean.grid.root as unknown as { data: { views: string[]; activeView: string } }).data;
    expect(leafData.views).toEqual(["teams-dashboard"]);
    expect(leafData.activeView).toBe("teams-dashboard");
  });

  it("sanitizer migrates a persisted teams-spend panel to Money in the Work tab", () => {
    // Money absorbed Payments/Products/Customers/Spend, so a layout saved
    // before that carries a panel id the registry no longer knows.
    const dirty = leafLayout(["teams-spend"], "teams-spend", {
      "teams-spend": {
        id: "teams-spend",
        contentComponent: "teams",
        params: { teamsKey: "spend" },
      },
    });
    const clean = sanitizeLayout(dirty);
    expect(clean.panels["teams-spend"]).toBeUndefined();
    expect(clean.panels["teams-work"]).toMatchObject({
      id: "teams-work",
      contentComponent: "teams",
      params: { teamsKey: "money" },
    });
  });

  it("sanitizer drops a retired panel instead of duplicating an already-open target", () => {
    const dirty = leafLayout(["teams-costs", "teams-dashboard"], "teams-costs", {
      "teams-costs": {
        id: "teams-costs",
        contentComponent: "teams",
        params: { teamsKey: "costs" },
      },
      "teams-dashboard": {
        id: "teams-dashboard",
        contentComponent: "teams",
        params: { teamsKey: "dashboard" },
      },
    });
    const clean = sanitizeLayout(dirty);
    expect(clean.panels["teams-costs"]).toBeUndefined();
    expect(Object.keys(clean.panels)).toEqual(["teams-dashboard"]);
    const leafData = (clean.grid.root as unknown as { data: { views: string[]; activeView: string } }).data;
    expect(leafData.views).toEqual(["teams-dashboard"]);
    // the dropped id was activeView — falls back to the remaining view
    expect(leafData.activeView).toBe("teams-dashboard");
  });

  it("sanitizer dedupes two retired panels that alias to the same absent target", () => {
    // Regression: teams-skills and teams-costs both alias to teams-dashboard.
    // Neither is already open — without target-collision tracking, both
    // would rename to "teams-dashboard" and the second write would silently
    // overwrite the first in `panels`, while the grid tree ends up with two
    // "teams-dashboard" view entries (a corrupt/duplicated dockview group).
    const dirty = leafLayout(["teams-skills", "teams-costs"], "teams-skills", {
      "teams-skills": {
        id: "teams-skills",
        contentComponent: "teams",
        params: { teamsKey: "skills" },
      },
      "teams-costs": {
        id: "teams-costs",
        contentComponent: "teams",
        params: { teamsKey: "costs" },
      },
    });
    const clean = sanitizeLayout(dirty);
    expect(clean.panels["teams-skills"]).toBeUndefined();
    expect(clean.panels["teams-costs"]).toBeUndefined();
    expect(Object.keys(clean.panels)).toEqual(["teams-dashboard"]);
    const leafData = (clean.grid.root as unknown as { data: { views: string[]; activeView: string } }).data;
    expect(leafData.views).toEqual(["teams-dashboard"]);
    expect(leafData.activeView).toBe("teams-dashboard");
  });

  it("sanitizer prunes a group emptied when its only tab is a dropped retired-panel duplicate", () => {
    // Regression (round-4 finding): teams-costs is the ONLY tab in group-1;
    // teams-dashboard (its alias target) is already open in group-2. The
    // duplicate-target branch drops teams-costs, which would otherwise
    // leave group-1 restored as an empty pane. It must be pruned, and the
    // branch (now down to one child) must collapse to group-2 rather than
    // persist a redundant single-child split.
    const dirty = {
      grid: {
        root: {
          type: "branch",
          size: 800,
          data: [
            { type: "leaf", size: 400, data: { id: "group-1", views: ["teams-costs"], activeView: "teams-costs" } },
            {
              type: "leaf",
              size: 400,
              data: { id: "group-2", views: ["teams-dashboard"], activeView: "teams-dashboard" },
            },
          ],
        },
        height: 600,
        width: 800,
        orientation: "HORIZONTAL" as SerializedDockview["grid"]["orientation"],
      },
      panels: {
        "teams-costs": { id: "teams-costs", contentComponent: "teams", params: { teamsKey: "costs" } },
        "teams-dashboard": {
          id: "teams-dashboard",
          contentComponent: "teams",
          params: { teamsKey: "dashboard" },
        },
      },
      activeGroup: "group-1",
    } as unknown as SerializedDockview;

    const clean = sanitizeLayout(dirty);

    expect(clean.panels["teams-costs"]).toBeUndefined();
    expect(Object.keys(clean.panels)).toEqual(["teams-dashboard"]);

    // The branch collapsed to its sole surviving child — no empty group-1
    // leaf anywhere in the tree, and no redundant single-child branch.
    expect(clean.grid.root.type).toBe("leaf");
    const leafData = (clean.grid.root as unknown as {
      data: { id: string; views: string[]; activeView: string };
    }).data;
    expect(leafData.id).toBe("group-2");
    expect(leafData.views).toEqual(["teams-dashboard"]);
    expect(leafData.activeView).toBe("teams-dashboard");

    // activeGroup pointed at the pruned group-1 — must be repointed off it.
    expect(clean.activeGroup).toBe("group-2");
  });

  function chatLayout(): SerializedDockview {
    return {
      grid: {
        root: {
          type: "branch",
          size: 800,
          data: [
            { type: "leaf", size: 400, data: { id: "g1", views: ["chat:friday"], activeView: "chat:friday" } },
            { type: "leaf", size: 400, data: { id: "g2", views: ["chat:atlas", "file-viewer"], activeView: "chat:atlas" } },
          ],
        },
        height: 600,
        width: 800,
        orientation: "HORIZONTAL" as SerializedDockview["grid"]["orientation"],
      },
      panels: {
        "chat:friday": { id: "chat:friday", contentComponent: "chat", params: { agentId: "friday" } },
        "chat:atlas": { id: "chat:atlas", contentComponent: "chat", title: "Atlas", params: { agentId: "atlas" } },
        "file-viewer": { id: "file-viewer", contentComponent: "file-viewer", params: {} },
      },
      activeGroup: "g2",
    } as unknown as SerializedDockview;
  }

  it("migrates the active legacy chat into Messages and keeps auxiliary panels", () => {
    const clean = sanitizeLayout(chatLayout(), ["friday", "atlas"]);
    expect(Object.keys(clean.panels).sort()).toEqual([MESSAGES_PANEL_ID, "file-viewer"].sort());
    expect(clean.panels[MESSAGES_PANEL_ID]).toMatchObject({
      id: MESSAGES_PANEL_ID, params: { agentId: "atlas" }, title: "Messages",
    });
    expect(clean.panels["file-viewer"]).toEqual(chatLayout().panels["file-viewer"]);
    expect(clean.grid.root.type).toBe("leaf");
    expect(clean.grid.root.data).toMatchObject({
      id: "g2", views: [MESSAGES_PANEL_ID, "file-viewer"], activeView: MESSAGES_PANEL_ID,
    });
    expect(clean.activeGroup).toBe("g2");
    expect(sanitizeLayout(clean, ["friday", "atlas"])).toEqual(clean);
  });

  it("falls back to the sentinel when the winning agent is absent from a settled roster", () => {
    const clean = sanitizeLayout(chatLayout(), ["friday"]);
    expect(clean.panels[MESSAGES_PANEL_ID].params).toEqual({ agentId: DEFAULT_AGENT_SENTINEL });
    expect(clean.panels["file-viewer"]).toBeDefined();
  });

  it("preserves the winner while the roster is unknown or empty", () => {
    for (const roster of [undefined, []]) {
      const clean = sanitizeLayout(chatLayout(), roster);
      expect(clean.panels[MESSAGES_PANEL_ID].params).toEqual({ agentId: "atlas" });
    }
  });

  it("prefers existing Messages when an auxiliary panel is active", () => {
    const dirty = leafLayout(["chat:friday", MESSAGES_PANEL_ID, "file-viewer"], "file-viewer", {
      "chat:friday": chatLayout().panels["chat:friday"],
      "file-viewer": chatLayout().panels["file-viewer"],
      [MESSAGES_PANEL_ID]: { id: MESSAGES_PANEL_ID, contentComponent: "chat", params: { agentId: "saved-agent" } },
    });
    const clean = sanitizeLayout(dirty);
    expect(clean.panels[MESSAGES_PANEL_ID].params).toEqual({ agentId: "saved-agent" });
    expect(clean.grid.root.data).toMatchObject({ views: [MESSAGES_PANEL_ID, "file-viewer"], activeView: "file-viewer" });
  });

  it("prefers the active legacy chat even when Messages already exists", () => {
    const dirty = leafLayout([MESSAGES_PANEL_ID, "chat:atlas"], "chat:atlas", {
      [MESSAGES_PANEL_ID]: { id: MESSAGES_PANEL_ID, contentComponent: "chat", params: { agentId: "friday" } },
      "chat:atlas": chatLayout().panels["chat:atlas"],
    });
    const clean = sanitizeLayout(dirty);
    expect(clean.panels[MESSAGES_PANEL_ID].params).toEqual({ agentId: "atlas" });
    expect(clean.grid.root.data).toMatchObject({ views: [MESSAGES_PANEL_ID], activeView: MESSAGES_PANEL_ID });
  });

  it("uses the first legacy chat ID when no selected chat or parameter is recoverable", () => {
    const dirty = leafLayout(["chat:friday", "file-viewer"], "file-viewer", {
      "chat:friday": { id: "chat:friday", contentComponent: "chat" },
      "file-viewer": chatLayout().panels["file-viewer"],
    });
    expect(sanitizeLayout(dirty).panels[MESSAGES_PANEL_ID].params).toEqual({ agentId: "friday" });
  });

  it("preserves a generic Messages selection across persistence", () => {
    const dirty = leafLayout([MESSAGES_PANEL_ID], MESSAGES_PANEL_ID, {
      [MESSAGES_PANEL_ID]: { id: MESSAGES_PANEL_ID, contentComponent: "chat", title: "Messages", params: {} },
    });
    expect(sanitizeLayout(dirty, ["friday"])).toEqual(dirty);
  });

  it("sanitizer keeps the default-agent sentinel panel (it resolves at render time)", () => {
    const dirty = leafLayout(["chat:__default__"], "chat:__default__", {
      "chat:__default__": {
        id: "chat:__default__",
        contentComponent: "chat",
        params: { agentId: "__default__" },
      },
    });
    const clean = sanitizeLayout(dirty, ["live"]);
    expect(clean.panels["chat:__default__"]).toBeDefined();
  });

  it("readLayout forwards the roster without dropping Messages", () => {
    writeLayout("u1", chatLayout());
    expect(readLayout("u1")?.panels[MESSAGES_PANEL_ID].params).toEqual({ agentId: "atlas" });
    expect(readLayout("u1", ["someone-else"])?.panels[MESSAGES_PANEL_ID].params)
      .toEqual({ agentId: DEFAULT_AGENT_SENTINEL });
  });

  it("clearLayout removes only its own key", () => {
    writeLayout("u1", minimalLayout({}));
    window.localStorage.setItem("unrelated", "x");
    clearLayout("u1");
    expect(readLayout("u1")).toBeNull();
    expect(window.localStorage.getItem("unrelated")).toBe("x");
  });
});
