import { describe, it, expect, beforeEach } from "vitest";
import type { SerializedDockview } from "dockview-react";
import {
  layoutStorageKey,
  readLayout,
  writeLayout,
  clearLayout,
  sanitizeLayout,
} from "../layoutStorage";

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
    expect(layoutStorageKey("org_1")).toBe("isol8.workspace.layout.v1:org_1");
  });

  it("round-trips a valid layout", () => {
    const layout = minimalLayout({
      "teams-inbox": { id: "teams-inbox", contentComponent: "teams", params: { teamsKey: "inbox" } },
    });
    writeLayout("u1", layout);
    expect(readLayout("u1")).toEqual(layout);
  });

  it("returns null on version mismatch or garbage", () => {
    window.localStorage.setItem(layoutStorageKey("u1"), JSON.stringify({ version: 2, layout: {} }));
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
    expect(clean.panels["teams-issues"].params).toEqual({ teamsKey: "issues", agent: "a-1" });
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

  // --- dead-agent chat panels ------------------------------------------
  // A wipe/reprovision leaves the persisted layout pointing at agent ids that
  // no longer exist. Pruning them is roster-gated: an in-flight roster must
  // NEVER be read as "these agents are gone".

  function chatLayout(): SerializedDockview {
    return {
      grid: {
        root: {
          type: "branch",
          size: 800,
          data: [
            { type: "leaf", size: 400, data: { id: "g1", views: ["chat:dead"], activeView: "chat:dead" } },
            { type: "leaf", size: 400, data: { id: "g2", views: ["chat:live"], activeView: "chat:live" } },
          ],
        },
        height: 600,
        width: 800,
        orientation: "HORIZONTAL" as SerializedDockview["grid"]["orientation"],
      },
      panels: {
        "chat:dead": { id: "chat:dead", contentComponent: "chat", params: { agentId: "dead" } },
        "chat:live": { id: "chat:live", contentComponent: "chat", params: { agentId: "live" } },
      },
      activeGroup: "g1",
    } as unknown as SerializedDockview;
  }

  it("sanitizer prunes chat panels whose agent is absent from a loaded roster", () => {
    const clean = sanitizeLayout(chatLayout(), ["live"]);

    expect(Object.keys(clean.panels)).toEqual(["chat:live"]);
    // Grid mirrors the drop: g1 is gone and the branch collapsed to g2.
    expect(clean.grid.root.type).toBe("leaf");
    expect((clean.grid.root as unknown as { data: { id: string } }).data.id).toBe("g2");
    expect(clean.activeGroup).toBe("g2");
  });

  it("sanitizer never prunes on an unknown roster (undefined or empty)", () => {
    // THE trap: the roster is empty for the whole first paint. Pruning on it
    // would nuke every chat panel on every reload.
    for (const roster of [undefined, []]) {
      const clean = sanitizeLayout(chatLayout(), roster);
      expect(Object.keys(clean.panels).sort()).toEqual(["chat:dead", "chat:live"]);
    }
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

  it("readLayout forwards the roster and reads back null when nothing survives", () => {
    writeLayout("u1", chatLayout());
    expect(Object.keys(readLayout("u1")?.panels ?? {}).sort()).toEqual(["chat:dead", "chat:live"]);
    expect(Object.keys(readLayout("u1", ["live"])?.panels ?? {})).toEqual(["chat:live"]);
    // Every panel pruned -> "no saved layout", so the shell builds the default
    // instead of restoring a grid whose views are all gone.
    expect(readLayout("u1", ["someone-else"])).toBeNull();
  });

  it("clearLayout removes only its own key", () => {
    writeLayout("u1", minimalLayout({}));
    window.localStorage.setItem("unrelated", "x");
    clearLayout("u1");
    expect(readLayout("u1")).toBeNull();
    expect(window.localStorage.getItem("unrelated")).toBe("x");
  });
});
