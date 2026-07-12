import { describe, it, expect, vi, beforeEach } from "vitest";
import type { DockviewApi, IDockviewPanel } from "dockview-react";
import {
  attachDockviewApi,
  openPanel,
  openChatPanel,
  closeSubjectDeleted,
  closeChatPanelIfOpen,
  isShellOperationInProgress,
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
    api = {
      setActive: vi.fn(() => {
        fire(this);
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
    addPanel: vi.fn((opts: { id: string; params?: Record<string, string> }) => {
      if (panels.has(opts.id)) throw new Error(`dockview: panel with id ${opts.id} already exists`);
      const p = new FakePanel(opts.id, opts.params ?? {});
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

  it("tracks visible chat agent ids from dockview groups", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => undefined);
    expect(_snapshotForTests().visibleChatAgentIds).toEqual([]);
    openChatPanel("a-1");
    expect(_snapshotForTests().visibleChatAgentIds).toEqual(["a-1"]);
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
