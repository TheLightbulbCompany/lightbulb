import { describe, it, expect, vi, beforeEach } from "vitest";
import type { DockviewApi, IDockviewPanel } from "dockview-react";
import {
  attachDockviewApi,
  openPanel,
  openChatPanel,
  closeSubjectDeleted,
  closeChatPanelIfOpen,
  isShellOperationInProgress,
  refreshWorkspaceSnapshot,
  _resetControllerForTests,
  _snapshotForTests,
} from "../workspaceController";

vi.mock("@/lib/shellNav", () => ({
  pushSearchParams: vi.fn(),
  replaceSearchParams: vi.fn(),
}));
import { pushSearchParams, replaceSearchParams } from "@/lib/shellNav";

type Handler<T> = (e: T) => void;

// Adaptation from the brief: the real DockviewApi exposes `activePanel` (a
// getter reflecting the last panel to become active) and `groups` (each
// group carries its own `activePanel`). The brief's fake omitted both, which
// silently zeroed out `recomputeSnapshot`'s reads of `_api.activePanel` — so
// this fake tracks the last-fired panel and surfaces it both ways.
function makeFakeApi() {
  const panels = new Map<string, FakePanel>();
  const activeChange: Handler<{ panel: FakePanel | undefined; origin: "user" | "api" }>[] = [];
  const removed: Handler<FakePanel>[] = [];
  let active: FakePanel | undefined;
  class FakePanel {
    title: string | undefined;
    api = {
      setActive: vi.fn(() => {
        fire(this);
      }),
      setTitle: vi.fn((t: string) => {
        this.title = t;
      }),
      updateParameters: vi.fn(),
      close: vi.fn(() => {
        panels.delete(this.id);
        if (active === this) active = undefined;
        removed.forEach((h) => h(this));
      }),
    };
    constructor(
      public id: string,
      public params: Record<string, string>,
    ) {}
  }
  function fire(panel: FakePanel | undefined) {
    active = panel;
    activeChange.forEach((h) => h({ panel, origin: "api" }));
  }
  const api = {
    getPanel: (id: string) => panels.get(id),
    addPanel: vi.fn((opts: { id: string; params?: Record<string, string>; title?: string }) => {
      if (panels.has(opts.id)) throw new Error(`dockview: panel with id ${opts.id} already exists`);
      const p = new FakePanel(opts.id, opts.params ?? {});
      p.title = opts.title;
      panels.set(opts.id, p);
      fire(p);
      return p;
    }),
    removePanel: vi.fn((p: FakePanel) => p.api.close()),
    clear: vi.fn(() => panels.clear()),
    onDidActivePanelChange: (h: Handler<{ panel: FakePanel | undefined; origin: "user" | "api" }>) => {
      activeChange.push(h);
      return { dispose: () => activeChange.splice(activeChange.indexOf(h), 1) };
    },
    onDidRemovePanel: (h: Handler<FakePanel>) => {
      removed.push(h);
      return { dispose: () => removed.splice(removed.indexOf(h), 1) };
    },
    onDidAddPanel: (_h: Handler<FakePanel>) => ({ dispose: vi.fn() }),
    onDidLayoutChange: (_h: Handler<void>) => ({ dispose: vi.fn() }),
    panels: [] as IDockviewPanel[],
    groups: [] as unknown[],
  };
  Object.defineProperty(api, "panels", { get: () => [...panels.values()] });
  Object.defineProperty(api, "activePanel", { get: () => active });
  Object.defineProperty(api, "groups", { get: () => [{ activePanel: active }] });
  return api as unknown as DockviewApi & { addPanel: ReturnType<typeof vi.fn>; removePanel: ReturnType<typeof vi.fn> };
}

beforeEach(() => {
  _resetControllerForTests();
  vi.clearAllMocks();
});

describe("workspaceController", () => {
  it("openPanel adds when missing, focuses when present, never duplicates", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => "friday-id");
    openPanel("teams-inbox");
    expect(api.addPanel).toHaveBeenCalledTimes(1);
    openPanel("teams-inbox");
    expect(api.addPanel).toHaveBeenCalledTimes(1); // focused, not re-added
  });

  it("focusing an existing panel re-delivers fresh durable params via updateParameters", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => undefined);
    openPanel("file-viewer", { path: "a.md" });
    expect(api.addPanel).toHaveBeenCalledTimes(1);
    // Opening file B while file A's viewer is open must actually switch files —
    // the focus path must re-deliver the descriptor's fresh durable params,
    // not just one-shots.
    openPanel("file-viewer", { path: "b.md" });
    expect(api.addPanel).toHaveBeenCalledTimes(1); // focused, not re-added
    const panel = api.getPanel("file-viewer") as unknown as {
      api: { updateParameters: ReturnType<typeof vi.fn> };
    };
    expect(panel.api.updateParameters).toHaveBeenCalledWith({ path: "b.md" });
  });

  it("re-opening an existing panel CLEARS clearable durable params absent from the new open", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => undefined);
    openPanel("teams-issues", { agent: "A" });
    // Plain (unfiltered) navigation to the already-open Tasks panel must
    // clear the stale agent filter with an explicit undefined — dockview's
    // updateParameters merges, so omission alone would keep the filter.
    openPanel("teams-issues", {});
    const panel = api.getPanel("teams-issues") as unknown as {
      api: { updateParameters: ReturnType<typeof vi.fn> };
    };
    // toEqual/toHaveBeenCalledWith ignore undefined-valued keys, but the
    // EXPLICIT `agent: undefined` is the whole fix (dockview deletes params
    // set to undefined) — assert own-property presence directly.
    const callArg = panel.api.updateParameters.mock.calls.at(-1)?.[0] as Record<string, unknown>;
    expect(callArg.teamsKey).toBe("issues");
    expect(Object.prototype.hasOwnProperty.call(callArg, "agent")).toBe(true);
    expect(callArg.agent).toBeUndefined();
  });

  it("re-opening with the SAME durable param re-delivers it (no spurious clear)", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => undefined);
    openPanel("teams-issues", { agent: "A" });
    openPanel("teams-issues", { agent: "B" });
    const panel = api.getPanel("teams-issues") as unknown as {
      api: { updateParameters: ReturnType<typeof vi.fn> };
    };
    expect(panel.api.updateParameters).toHaveBeenCalledWith({
      teamsKey: "issues",
      agent: "B",
    });
  });

  it("focusing an existing panel heals a stale persisted tab title", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => "friday-id");
    openPanel("teams-artifacts");
    const panel = api.getPanel("teams-artifacts") as unknown as {
      title: string | undefined;
      api: { setTitle: ReturnType<typeof vi.fn> };
    };
    // As restored by fromJSON from a blob saved before the rename.
    panel.title = "Artifacts";
    openPanel("teams-artifacts");
    expect(panel.api.setTitle).toHaveBeenCalledWith("Library");
    panel.api.setTitle.mockClear();
    openPanel("teams-artifacts");
    expect(panel.api.setTitle).not.toHaveBeenCalled(); // already canonical
  });

  it("openPanel with pushUrl writes the canonical URL once", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => undefined);
    openPanel("teams-issue-detail", { detailId: "ADM-1" }, { pushUrl: true });
    expect(pushSearchParams).toHaveBeenCalledTimes(1);
    const arg = (pushSearchParams as ReturnType<typeof vi.fn>).mock.calls[0][0] as URLSearchParams;
    expect(arg.get("panel")).toBe("teams-issue-detail");
    expect(arg.get("detailId")).toBe("ADM-1");
  });

  it("openChatPanel unifies sentinel and concrete ids", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => "friday-id");
    openPanel("chat", { agent: "__default__" }); // sentinel panel exists (default layout)
    expect(api.getPanel("chat:__default__")).toBeDefined();
    openChatPanel("friday-id"); // concrete id that RESOLVES to the sentinel → focus it, no new panel
    expect(api.addPanel).toHaveBeenCalledTimes(1);
    openChatPanel("other-agent"); // different agent → new panel
    expect(api.addPanel).toHaveBeenCalledTimes(2);
    expect(api.getPanel("chat:other-agent")).toBeDefined();
  });

  it("chat panels are added with renderer always", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => undefined);
    openChatPanel("a-1");
    expect(api.addPanel).toHaveBeenCalledWith(expect.objectContaining({ renderer: "always" }));
  });

  it("closeSubjectDeleted replaces URL, closes self with suppression up, opens the list", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => undefined);
    openPanel("teams-issue-detail", { detailId: "ADM-1" });
    let suppressedDuringClose = false;
    api.onDidRemovePanel(() => {
      suppressedDuringClose = isShellOperationInProgress();
    });
    closeSubjectDeleted("teams-issue-detail:ADM-1", "teams-issues");
    expect(replaceSearchParams).toHaveBeenCalled(); // URL → list, replace not push
    expect(suppressedDuringClose).toBe(true);       // clear-on-close listener must skip
    expect(api.getPanel("teams-issue-detail:ADM-1")).toBeUndefined();
    expect(api.getPanel("teams-issues")).toBeDefined();
    expect(isShellOperationInProgress()).toBe(false);
  });

  it("tracks focused chat agent in the snapshot", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => "friday-id");
    openChatPanel("a-1");
    expect(_snapshotForTests().focusedChatAgentId).toBe("a-1");
    openPanel("teams-inbox");
    expect(_snapshotForTests().focusedChatAgentId).toBeNull();
    expect(_snapshotForTests().activePanelId).toBe("teams-inbox");
  });

  it("lastFocusedChatAgentId is sticky across focus moving to a non-chat panel", () => {
    // dockview activates a freshly added panel, so opening the file viewer
    // from a chat makes the viewer itself the active panel — the momentary
    // focusedChatAgentId null must not lose which chat opened it.
    const api = makeFakeApi();
    attachDockviewApi(api, () => "friday-id");
    openChatPanel("a-1");
    expect(_snapshotForTests().focusedChatAgentId).toBe("a-1");
    expect(_snapshotForTests().lastFocusedChatAgentId).toBe("a-1");
    openPanel("teams-inbox");
    expect(_snapshotForTests().focusedChatAgentId).toBeNull();
    expect(_snapshotForTests().lastFocusedChatAgentId).toBe("a-1");
    // Focusing a DIFFERENT chat panel re-points the sticky field.
    openChatPanel("a-2");
    expect(_snapshotForTests().lastFocusedChatAgentId).toBe("a-2");
  });

  it("tracks visible chat agent ids from dockview groups", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => undefined);
    expect(_snapshotForTests().visibleChatAgentIds).toEqual([]);
    openChatPanel("a-1");
    expect(_snapshotForTests().visibleChatAgentIds).toEqual(["a-1"]);
  });

  it("dedupes visibleChatAgentIds when a sentinel and its concrete panel are both visible", () => {
    // A restored layout can hold the sentinel chat panel (chat:__default__,
    // resolving to friday-id) and the concrete chat:friday-id panel in separate
    // groups. Both resolve to the same agent — the snapshot must list it once.
    const sentinel = { id: "chat:__default__" };
    const concrete = { id: "chat:friday-id" };
    const api = {
      activePanel: concrete,
      panels: [sentinel, concrete],
      groups: [{ activePanel: sentinel }, { activePanel: concrete }],
      getPanel: () => undefined,
      onDidActivePanelChange: () => ({ dispose: vi.fn() }),
      onDidAddPanel: () => ({ dispose: vi.fn() }),
      onDidRemovePanel: () => ({ dispose: vi.fn() }),
      onDidLayoutChange: () => ({ dispose: vi.fn() }),
    } as unknown as DockviewApi;
    attachDockviewApi(api, () => "friday-id");
    expect(_snapshotForTests().visibleChatAgentIds).toEqual(["friday-id"]);
  });

  it("refreshWorkspaceSnapshot re-resolves the sentinel when the roster arrives late", () => {
    // Cold load: the sentinel chat panel opens before useAgents().defaultId
    // exists, so the snapshot can't resolve it. The roster arriving is not a
    // dockview event — the shell must call refreshWorkspaceSnapshot to
    // re-resolve, or sidebar highlight / SessionList / toast suppression stay
    // broken until some unrelated layout event.
    const api = makeFakeApi();
    let rosterDefaultId: string | undefined = undefined; // roster not loaded yet
    attachDockviewApi(api, () => rosterDefaultId);
    openPanel("chat", { agent: "__default__" }); // Mission Control sentinel
    expect(_snapshotForTests().focusedChatAgentId).toBeNull();
    expect(_snapshotForTests().visibleChatAgentIds).toEqual([]);
    rosterDefaultId = "friday-id"; // roster arrives
    refreshWorkspaceSnapshot();
    expect(_snapshotForTests().focusedChatAgentId).toBe("friday-id");
    expect(_snapshotForTests().visibleChatAgentIds).toContain("friday-id");
  });

  it("a recompute that changes nothing keeps the previous snapshot object", () => {
    // Identity is the change signal useSyncExternalStore reads (Object.is), and
    // onDidLayoutChange fires for moves that touch no snapshot field — a fresh
    // object each time would re-render every subscriber on every dock event.
    const api = makeFakeApi();
    attachDockviewApi(api, () => "friday-id");
    openChatPanel("a-1");
    const before = _snapshotForTests();
    refreshWorkspaceSnapshot();
    expect(_snapshotForTests()).toBe(before);
    // A real change still publishes a new object.
    openPanel("teams-inbox");
    expect(_snapshotForTests()).not.toBe(before);
  });

  it("teamsPanelVisible requires a teams panel to be some group's ACTIVE tab, not merely open", () => {
    // Mission Control keeps teams panels permanently OPEN — if open counted
    // as visible, teams run toasts would be suppressed forever on the
    // default layout.
    const teams = { id: "teams-inbox" };
    const chatA = { id: "chat:a-1" };
    const stubs = {
      getPanel: () => undefined,
      onDidActivePanelChange: () => ({ dispose: vi.fn() }),
      onDidAddPanel: () => ({ dispose: vi.fn() }),
      onDidRemovePanel: () => ({ dispose: vi.fn() }),
      onDidLayoutChange: () => ({ dispose: vi.fn() }),
    };
    // Teams panel open but a BACKGROUND tab (its group's active tab is chat):
    const detach = attachDockviewApi(
      {
        ...stubs,
        activePanel: chatA,
        panels: [teams, chatA],
        groups: [{ activePanel: chatA }],
      } as unknown as DockviewApi,
      () => undefined,
    );
    expect(_snapshotForTests().openPanelIds).toContain("teams-inbox");
    expect(_snapshotForTests().teamsPanelVisible).toBe(false);
    detach();
    // Teams panel is the active tab of a BACKGROUND group (dock focus on chat):
    attachDockviewApi(
      {
        ...stubs,
        activePanel: chatA,
        panels: [teams, chatA],
        groups: [{ activePanel: chatA }, { activePanel: teams }],
      } as unknown as DockviewApi,
      () => undefined,
    );
    expect(_snapshotForTests().teamsPanelVisible).toBe(true);
  });

  it("closeChatPanelIfOpen closes an open chat panel under the shell-op guard", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => undefined);
    openChatPanel("a-1");
    expect(api.getPanel("chat:a-1")).toBeDefined();
    let suppressedDuringClose = false;
    api.onDidRemovePanel(() => {
      suppressedDuringClose = isShellOperationInProgress();
    });
    closeChatPanelIfOpen("a-1");
    expect(api.getPanel("chat:a-1")).toBeUndefined();
    expect(suppressedDuringClose).toBe(true);
    expect(isShellOperationInProgress()).toBe(false);
  });

  it("closeChatPanelIfOpen is a no-op when the panel isn't open", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => undefined);
    expect(() => closeChatPanelIfOpen("never-opened")).not.toThrow();
    expect(api.removePanel).not.toHaveBeenCalled();
  });
});
