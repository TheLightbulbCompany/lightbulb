import { describe, it, expect, vi, beforeEach } from "vitest";
import type { DockviewApi, IDockviewPanel } from "dockview-react";
import {
  attachDockviewApi,
  openPanel,
  openChatPanel,
  openChatPanelWithDraft,
  openMessagePanel,
  openSessionPanel,
  closeSubjectDeleted,
  activateMessagesPanel,
  syncMessagesPanelAgent,
  isShellOperationInProgress,
  refreshWorkspaceSnapshot,
  _resetControllerForTests,
  _snapshotForTests,
} from "../workspaceController";
import { MESSAGES_PANEL_ID } from "../panelRegistry";
import { parseCommand } from "../urlChannel";

vi.mock("@/lib/shellNav", () => ({
  pushSearchParams: vi.fn(),
  replaceSearchParams: vi.fn(),
}));
import { pushSearchParams, replaceSearchParams } from "@/lib/shellNav";
import {
  consumeFocusComposerRequest,
  releaseFocusComposerRequest,
  _resetFocusComposerRequestForTests,
} from "@/lib/focusComposerRequest";

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
      updateParameters: vi.fn((next: Record<string, string | undefined>) => {
        for (const [key, value] of Object.entries(next)) {
          if (value === undefined) delete this.params[key];
          else this.params[key] = value;
        }
      }),
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
    onDidAddPanel: () => ({ dispose: vi.fn() }),
    onDidLayoutChange: () => ({ dispose: vi.fn() }),
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
  _resetFocusComposerRequestForTests();
  vi.clearAllMocks();
});

describe("workspaceController", () => {
  it("retargets the Messages panel for a session, bumping sessionIntent on repeat opens, and never duplicates it", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => "friday-id");
    openSessionPanel("s1");
    expect(api.addPanel).toHaveBeenCalledTimes(1);
    expect(api.getPanel(MESSAGES_PANEL_ID)?.params).toEqual({ sessionId: "s1", sessionIntent: "1" });
    openSessionPanel("s1");
    expect(api.addPanel).toHaveBeenCalledTimes(1); // focused, not a second panel
    expect(api.getPanel(MESSAGES_PANEL_ID)?.params).toEqual({ sessionId: "s1", sessionIntent: "2" });
  });
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
    const panel = api.getPanel("teams-work") as unknown as {
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
    const panel = api.getPanel("teams-work") as unknown as {
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

  it("retargets one Messages panel for every agent open", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => "friday-id");
    openPanel("chat", { agent: "__default__" });
    openChatPanel("friday-id");
    openChatPanel("other-agent");
    expect(api.addPanel).toHaveBeenCalledTimes(1);
    expect(api.getPanel(MESSAGES_PANEL_ID)?.params).toEqual({ agentId: "other-agent" });
    expect(_snapshotForTests().focusedChatAgentId).toBe("other-agent");
    expect(_snapshotForTests().visibleChatAgentIds).toEqual(["other-agent"]);
  });

  it("clears the agent target when a generic room activates Messages", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => "friday-id");
    openChatPanel("friday-id");
    activateMessagesPanel();
    expect(api.addPanel).toHaveBeenCalledTimes(1);
    expect(api.getPanel(MESSAGES_PANEL_ID)?.api.updateParameters)
      .toHaveBeenLastCalledWith({ agentId: undefined, agentSelection: "resolved" });
    expect(api.getPanel(MESSAGES_PANEL_ID)?.params).toEqual({ agentSelection: "resolved" });
    expect(_snapshotForTests().focusedChatAgentId).toBeNull();
    expect(_snapshotForTests().visibleChatAgentIds).toEqual([]);
    syncMessagesPanelAgent("other-agent");
    expect(_snapshotForTests().focusedChatAgentId).toBe("other-agent");
  });

  it("syncs resolved metadata without focus and clears it for an explicit same-agent command", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => undefined);
    openChatPanel("a-1");
    openPanel("file-viewer");
    syncMessagesPanelAgent("a-2");
    const panel = api.getPanel(MESSAGES_PANEL_ID)!;
    expect(panel.params).toEqual({ agentId: "a-2", agentSelection: "resolved" });
    expect(_snapshotForTests().activePanelId).toBe("file-viewer");
    const snapshot = _snapshotForTests();
    vi.mocked(panel.api.updateParameters).mockClear();
    syncMessagesPanelAgent("a-2");
    expect(panel.api.updateParameters).not.toHaveBeenCalled();
    expect(_snapshotForTests()).toBe(snapshot);
    openChatPanel("a-2");
    expect(panel.params).toEqual({ agentId: "a-2" });
    expect(_snapshotForTests().focusedChatAgentId).toBe("a-2");
  });

  it("publishes parameter retargeting even when setActive emits no event", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => undefined);
    openChatPanel("a-1");
    vi.mocked(api.getPanel(MESSAGES_PANEL_ID)!.api.setActive).mockImplementation(() => {});
    openChatPanel("a-2");
    expect(_snapshotForTests().focusedChatAgentId).toBe("a-2");
    expect(_snapshotForTests().lastFocusedChatAgentId).toBe("a-2");
  });

  it("chat panels are added with renderer always", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => undefined);
    openChatPanel("a-1");
    expect(api.addPanel).toHaveBeenCalledWith(expect.objectContaining({ renderer: "always" }));
  });

  it("openMessagePanel retargets the Messages panel for a room permalink and clears the stale agent target", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => "friday-id");
    openChatPanel("friday-id");
    openMessagePanel("room-1", "msg-1");
    expect(api.addPanel).toHaveBeenCalledTimes(1); // retargeted, not a second panel
    expect(api.getPanel(MESSAGES_PANEL_ID)?.api.updateParameters)
      .toHaveBeenLastCalledWith({ roomId: "room-1", messageId: "msg-1", agentId: undefined, agentSelection: undefined });
    expect(api.getPanel(MESSAGES_PANEL_ID)?.params).toEqual({ roomId: "room-1", messageId: "msg-1" });
  });

  it("openMessagePanel opens a room without a target message", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => undefined);
    openMessagePanel("room-2");
    expect(api.getPanel(MESSAGES_PANEL_ID)?.params).toEqual({ roomId: "room-2" });
  });

  // Proves the WorkspaceShell routing-gap fix: a ?panel=chat&room=…&message=…
  // command must reach the Messages panel's params. WorkspaceShell.test.tsx
  // mocks getWorkspaceSnapshot to `{ ready: false }`, so it never exercises the
  // reconciler's chat branch — this exercises the controller side of the same
  // seam WorkspaceShell.tsx's reconciler calls into (`command.descriptor.params
  // .roomId ? openMessagePanel(...) : openChatPanel(...)`).
  it("a room permalink command reaches the Messages panel via openMessagePanel, not the agentId-only openChatPanel", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => undefined);
    const command = parseCommand("?panel=chat&room=r1&message=m1");
    expect(command?.descriptor.component).toBe("chat");
    if (command?.descriptor.params.roomId) {
      openMessagePanel(command.descriptor.params.roomId, command.descriptor.params.messageId);
    } else {
      openChatPanel(command?.descriptor.params.agentId as string);
    }
    expect(api.getPanel(MESSAGES_PANEL_ID)?.params).toEqual({ roomId: "r1", messageId: "m1" });
  });

  it("openChatPanelWithDraft defers the draft until room navigation settles", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => "a-1");
    expect(openChatPanelWithDraft("file the inbox")).toBe(true);
    expect(api.addPanel).toHaveBeenCalledWith(expect.objectContaining({ id: MESSAGES_PANEL_ID, params: { agentId: "a-1" } }));
    expect(consumeFocusComposerRequest("a-1")).toBeNull();
    expect(releaseFocusComposerRequest("a-1")).toBe(true);
    expect(consumeFocusComposerRequest("a-1")).toEqual({ draft: "file the inbox" });
  });

  it("openChatPanelWithDraft is a silent no-op with no default agent", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => undefined);
    expect(openChatPanelWithDraft("x")).toBe(false);
    expect(api.addPanel).not.toHaveBeenCalled();
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
    expect(api.getPanel("teams-work")).toBeDefined();
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

  it("reads only the singleton's parameter for visible agent snapshots", () => {
    const sentinel = { id: MESSAGES_PANEL_ID, params: { agentId: "other-agent" } };
    const concrete = { id: "chat:friday-id", params: { agentId: "friday-id" } };
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
    expect(_snapshotForTests().visibleChatAgentIds).toEqual(["other-agent"]);
    expect(_snapshotForTests().focusedChatAgentId).toBeNull();
  });

  it("refreshWorkspaceSnapshot re-resolves the sentinel when the roster arrives late", () => {
    // Cold load: the sentinel chat panel opens before useAgents().defaultId
    // exists, so the snapshot can't resolve it. The roster arriving is not a
    // dockview event — the shell must call refreshWorkspaceSnapshot to
    // re-resolve, or sidebar highlight / toast suppression stay
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

  it("can open Messages with no agent selected", () => {
    const api = makeFakeApi();
    attachDockviewApi(api, () => undefined);
    activateMessagesPanel();
    expect(api.getPanel(MESSAGES_PANEL_ID)?.params).toEqual({ agentSelection: "resolved" });
    expect(_snapshotForTests().focusedChatAgentId).toBeNull();
  });
});
