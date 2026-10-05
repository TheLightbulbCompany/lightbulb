// Module-level singleton owning the DockviewApi. The repo's store pattern
// (useIsDesktop): module state + Set of listeners + useSyncExternalStore.
// EVERY programmatic open/focus goes through openPanel/openChatPanel — a
// future toast or agent event that bypasses this API silently loses URL
// integration, so nothing else may call api.addPanel.
"use client";

import { useSyncExternalStore } from "react";
// dockview-core's index re-exports the lifecycle IDisposable interface under
// the prefixed name `DockviewIDisposable` (see dockview-core/dist/esm/index.d.ts)
// — there is no plain `IDisposable` export, unlike the brief's import list.
import type { DockviewApi, IDockviewPanel, DockviewIDisposable as IDisposable } from "dockview-react";
import { pushSearchParams, replaceSearchParams } from "@/lib/shellNav";
import { deferFocusComposerRequest, requestFocusComposer } from "@/lib/focusComposerRequest";
import {
  describePanel,
  MESSAGES_PANEL_ID,
  clearablePanelParamKeys,
  DEFAULT_AGENT_SENTINEL,
  type PanelDescriptor,
} from "./panelRegistry";
import { buildPanelUrl } from "./urlChannel";
import { readTasksPlacement, writeTasksPlacement, type TasksPlacement } from "./layoutStorage";

export interface WorkspaceSnapshot {
  ready: boolean;
  activePanelId: string | null;
  focusedChatAgentId: string | null; // sentinel RESOLVED when possible
  // Sticky variant of focusedChatAgentId: the last chat agent that WAS
  // focused, surviving focus moving to a non-chat panel (dockview activates a
  // newly added panel, so the file viewer opened from a chat is itself the
  // active panel — the momentary null must not resolve it to the default
  // agent). Never reset to null while the dock is attached.
  lastFocusedChatAgentId: string | null;
  visibleChatAgentIds: string[]; // one entry per dockview group whose active panel is a chat panel
  teamsPanelVisible: boolean;
  // Messages is the active tab of SOME group — true while Tasks is focused
  // beside it, when activePanelId is Tasks but the conversation is on screen.
  messagesVisible: boolean;
  /** The Browser panel is the active tab of some group (open behind Messages is not visible). */
  browserVisible: boolean;
  openPanelIds: string[];
}

export const BROWSER_PANEL_ID = "browser";

let _api: DockviewApi | null = null;
let _resolveDefaultAgentId: () => string | undefined = () => undefined;
let _shellOpDepth = 0;
let _snapshot: WorkspaceSnapshot = emptySnapshot();
const _listeners = new Set<() => void>();

function emptySnapshot(): WorkspaceSnapshot {
  return {
    ready: false,
    activePanelId: null,
    focusedChatAgentId: null,
    lastFocusedChatAgentId: null,
    visibleChatAgentIds: [],
    teamsPanelVisible: false,
    messagesVisible: false,
    browserVisible: false,
    openPanelIds: [],
  };
}

function notify(): void {
  _listeners.forEach((l) => l());
}

function sameIds(a: readonly string[], b: readonly string[]): boolean {
  return a.length === b.length && a.every((id, i) => id === b[i]);
}

function sameSnapshot(a: WorkspaceSnapshot, b: WorkspaceSnapshot): boolean {
  return (
    a.ready === b.ready &&
    a.activePanelId === b.activePanelId &&
    a.focusedChatAgentId === b.focusedChatAgentId &&
    a.lastFocusedChatAgentId === b.lastFocusedChatAgentId &&
    a.teamsPanelVisible === b.teamsPanelVisible &&
    a.messagesVisible === b.messagesVisible &&
    a.browserVisible === b.browserVisible &&
    sameIds(a.visibleChatAgentIds, b.visibleChatAgentIds) &&
    sameIds(a.openPanelIds, b.openPanelIds)
  );
}

// Object identity IS the change signal: useSyncExternalStore compares with
// Object.is, so publishing a fresh-but-equal object re-renders every subscriber
// on every dockview event — and onDidLayoutChange fires for moves that don't
// touch a single snapshot field. Keep the previous object when nothing moved,
// and skip the notify entirely (no subscriber has anything to re-read).
function commitSnapshot(next: WorkspaceSnapshot): void {
  if (sameSnapshot(_snapshot, next)) return;
  _snapshot = next;
  notify();
}

function agentIdOfChatPanel(panel: IDockviewPanel | undefined): string | null {
  if (panel?.id !== MESSAGES_PANEL_ID) return null;
  const raw = panel.params?.agentId;
  if (typeof raw !== "string" || !raw) return null;
  if (raw === DEFAULT_AGENT_SENTINEL) return _resolveDefaultAgentId() ?? null;
  return raw;
}

function recomputeSnapshot(): void {
  if (!_api) {
    commitSnapshot(emptySnapshot());
    return;
  }
  const active = _api.activePanel;
  const openPanelIds = _api.panels.map((p) => p.id);
  const groupActivePanels = _api.groups.map((g) => g.activePanel);
  const visibleChatAgentIds = Array.from(
    new Set(
      groupActivePanels
        .map((p) => agentIdOfChatPanel(p))
        .filter((id): id is string => id !== null),
    ),
  );
  const focusedChatAgentId = agentIdOfChatPanel(active);
  commitSnapshot({
    ready: true,
    activePanelId: active?.id ?? null,
    focusedChatAgentId,
    // Sticky: keep the previous value while a non-chat panel is focused.
    lastFocusedChatAgentId: focusedChatAgentId ?? _snapshot.lastFocusedChatAgentId,
    visibleChatAgentIds,
    // Visible = the ACTIVE tab of some group (same traversal as
    // visibleChatAgentIds), NOT merely open: Mission Control keeps teams
    // panels permanently open, so open-based suppression would swallow every
    // teams run toast on the default layout.
    teamsPanelVisible: groupActivePanels.some(
      (p) => p !== undefined && p.id.startsWith("teams-"),
    ),
    messagesVisible: groupActivePanels.some((p) => p?.id === MESSAGES_PANEL_ID),
    browserVisible: groupActivePanels.some((p) => p?.id === BROWSER_PANEL_ID),
    openPanelIds,
  });
}

export function attachDockviewApi(
  api: DockviewApi,
  resolveDefaultAgentId: () => string | undefined,
): () => void {
  _api = api;
  _resolveDefaultAgentId = resolveDefaultAgentId;
  const disposables: IDisposable[] = [
    api.onDidActivePanelChange(() => recomputeSnapshot()),
    api.onDidAddPanel(() => recomputeSnapshot()),
    api.onDidRemovePanel(() => recomputeSnapshot()),
    // A background group's active-panel change (drag-move a tab between groups,
    // reorder tabs) fires none of the above but changes which chat panels are
    // the active tab of a visible group — so visibleChatAgentIds must refresh.
    api.onDidLayoutChange(() => recomputeSnapshot()),
  ];
  // After the dock has laid itself out at the new size, not before.
  const onResize = () => requestAnimationFrame(refitSplits);
  window.addEventListener("resize", onResize);
  recomputeSnapshot();
  return () => {
    window.removeEventListener("resize", onResize);
    disposables.forEach((d) => d.dispose());
    _api = null;
    _resolveDefaultAgentId = () => undefined;
    recomputeSnapshot();
  };
}

// The snapshot's sentinel resolution (chat:__default__ → concrete agent id)
// reads the default-agent resolver at recompute time, but the roster arriving
// is not a dockview event — none of the attach-time listeners fire. The shell
// calls this when the resolved default id changes so a cold load re-resolves
// focusedChatAgentId / visibleChatAgentIds. Safe no-op when no api is
// attached (the snapshot is already the empty one).
export function refreshWorkspaceSnapshot(): void {
  recomputeSnapshot();
}

function addOrFocus(descriptor: PanelDescriptor, extraParams?: Record<string, string>): void {
  if (!_api) return;
  const existing = _api.getPanel(descriptor.panelId);
  if (existing) {
    // Re-open of an already-open panel must re-deliver BOTH the descriptor's
    // fresh durable params (opening file B while file A's viewer is open has
    // to actually switch files) AND any one-shots (a second ?new=task
    // command). Panels watch these via onDidParametersChange. Merge semantics:
    // updateParameters merges, so a clearable durable param (an agent filter,
    // an inspector tab) that this open does NOT carry must be cleared with an
    // explicit `undefined` — a plain navigation to an already-open Tasks
    // panel shows the UNFILTERED list, matching the mobile invariant that
    // plain panel nav drops stale ?agent/detail state.
    const nextParams: Record<string, string | undefined> = {
      ...descriptor.params,
      ...extraParams,
    };
    for (const k of clearablePanelParamKeys(descriptor.panelId)) {
      if (!(k in nextParams)) nextParams[k] = undefined;
    }
    if (Object.keys(nextParams).length > 0) {
      existing.api.updateParameters(nextParams);
    }
    // A panel restored from a persisted layout keeps whatever title was
    // serialized at save time — re-assert the registry title so a rename
    // reaches long-lived layouts.
    if (existing.title !== descriptor.title) {
      existing.api.setTitle(descriptor.title);
    }
    existing.api.setActive();
    recomputeSnapshot();
    return;
  }
  const split = descriptor.panelId === TASKS_PANEL_ID && canSplitBesideMessages()
    && tasksPlacement() === "split";
  _api.addPanel({
    id: descriptor.panelId,
    component: descriptor.component,
    title: descriptor.title,
    params: { ...descriptor.params, ...extraParams },
    ...(descriptor.renderer ? { renderer: descriptor.renderer } : {}),
    // Tasks opens beside the conversation rather than as a tab that hides it.
    ...(split
      ? {
          position: { referencePanel: MESSAGES_PANEL_ID, direction: "right" as const },
          initialWidth: Math.round(_api.width * TASKS_SPLIT_FRACTION),
        }
      : {}),
  });
}

// The Tasks list panel is the only one that splits. Its detail panels
// (teams-issues:<id>) keep today's tab behaviour.
export const TASKS_PANEL_ID = "teams-issues";
const TASKS_SPLIT_FRACTION = 0.4;
// ponytail: a fixed floor on the dock width, not a measured content minimum.
// Below it the conversation and the list would both be too cramped to read,
// so Tasks opens as a tab exactly as it did before the split existed.
export const MIN_SPLIT_DOCK_WIDTH = 720;

export function canSplitBesideMessages(): boolean {
  return !!_api?.getPanel(MESSAGES_PANEL_ID) && _api.width >= MIN_SPLIT_DOCK_WIDTH;
}

/**
 * Show the shared Browser next to the conversation (an agent started browsing, or a person
 * clicked its live thumbnail). An already-open panel is only brought forward when `focus`;
 * a narrow dock opens it as a tab, and only when asked (`asTabWhenNarrow`), so an agent's
 * browsing never swaps the conversation out from under the person reading it.
 * Returns false when nothing opened (no dock, or narrow without `asTabWhenNarrow`).
 */
export function showBrowserBesideChat(opts: { focus?: boolean; asTabWhenNarrow?: boolean } = {}): boolean {
  if (!_api) return false;
  const existing = _api.getPanel(BROWSER_PANEL_ID);
  if (existing) {
    const messages = _api.getPanel(MESSAGES_PANEL_ID);
    // A restored layout often keeps Browser as a tab behind Messages: bring it out beside
    // the conversation, which is what "open beside the chat" means.
    if (messages && existing.group === messages.group && canSplitBrowser()) {
      _shellOpDepth += 1;
      try {
        existing.api.moveTo({ group: messages.group, position: "right" });
      } finally {
        _shellOpDepth -= 1;
      }
      existing.group.api.setSize({ width: browserSplitWidth(_api.width) });
      messages.api.setActive();
      if (opts.focus) existing.api.setActive();
    } else if (opts.focus || !_snapshot.browserVisible) {
      existing.api.setActive();
    }
    recomputeSnapshot();
    return true;
  }
  const descriptor = describePanel(BROWSER_PANEL_ID, {});
  if (!descriptor) return false;
  if (!canSplitBrowser()) {
    if (!opts.asTabWhenNarrow) return false;
    addOrFocus(descriptor);
    return true;
  }
  _api.addPanel({
    id: descriptor.panelId,
    component: descriptor.component,
    title: descriptor.title,
    params: descriptor.params,
    position: { referencePanel: MESSAGES_PANEL_ID, direction: "right" },
    initialWidth: browserSplitWidth(_api.width),
  });
  recomputeSnapshot();
  return true;
}

/** Two columns: the room never narrower than this, the browser never narrower than that. A
 * thread goes UNDER the browser (useBrowserThreadHost), so it needs no width of its own. */
export const MIN_ROOM_WIDTH = 560;
export const MIN_BROWSER_WIDTH = 640;

function canSplitBrowser(): boolean {
  return !!_api?.getPanel(MESSAGES_PANEL_ID) && _api.width >= MIN_ROOM_WIDTH + MIN_BROWSER_WIDTH;
}

/** Half the dock, but never below either column's minimum. */
export function browserSplitWidth(dockWidth: number): number {
  return Math.round(Math.min(dockWidth - MIN_ROOM_WIDTH, Math.max(MIN_BROWSER_WIDTH, dockWidth / 2)));
}

/**
 * Keep the two columns readable as the window resizes: a dock too narrow for both folds the
 * Browser into the conversation's group as a tab (the conversation stays in front), and a
 * column squeezed under its minimum gets its split back.
 */
export function refitBrowserSplit(): void {
  const browser = _api?.getPanel(BROWSER_PANEL_ID);
  const messages = _api?.getPanel(MESSAGES_PANEL_ID);
  if (!_api || !browser || !messages || browser.group === messages.group) return;
  if (!canSplitBrowser()) {
    _shellOpDepth += 1;
    try {
      browser.api.moveTo({ group: messages.group, position: "center" });
    } finally {
      _shellOpDepth -= 1;
    }
    messages.api.setActive();
    recomputeSnapshot();
    return;
  }
  const width = browser.group.api.width;
  if (width < MIN_BROWSER_WIDTH || messages.group.api.width < MIN_ROOM_WIDTH) {
    browser.group.api.setSize({ width: browserSplitWidth(_api.width) });
  }
}

// The choice made in this page. Storage can refuse the write (privacy mode, quota), and an
// Expand must still hold for the rest of the session rather than read back as "split".
let _sessionTasksPlacement: TasksPlacement | null = null;
const tasksPlacement = (): TasksPlacement => _sessionTasksPlacement ?? readTasksPlacement();

/**
 * Keep Tasks where the dock's width allows. Too narrow for Tasks beside the conversation:
 * fold it into the conversation's group as a tab. Wide again, and the stored placement
 * still says split: put it back beside (the fold is persisted with the layout, so nothing
 * else would). Whatever panel was active stays active (it may be a third tab, such as
 * Browser or Mail), and the stored placement is never written here: a person's own Expand
 * keeps Tasks a tab at any width.
 */
function refitTasksSplit(): void {
  const tasks = _api?.getPanel(TASKS_PANEL_ID);
  const messages = _api?.getPanel(MESSAGES_PANEL_ID);
  if (!_api || !tasks || !messages) return;
  const folded = tasks.group === messages.group;
  const fits = canSplitBesideMessages();
  if (folded ? !(fits && tasksPlacement() === "split") : fits) return;
  const active = _api.activePanel;
  _shellOpDepth += 1;
  try {
    tasks.api.moveTo({ group: messages.group, position: folded ? "right" : "center" });
  } finally {
    _shellOpDepth -= 1;
  }
  active?.api.setActive();
  recomputeSnapshot();
}

/** Refit both splits to the dock's width: on a window resize, and once after a layout restore. */
export function refitSplits(): void {
  // Not laid out yet: folding at width 0 would undo a split the dock has room for.
  if (!_api?.width) return;
  refitBrowserSplit();
  refitTasksSplit();
}

// Expand (split → tab) / Show beside chat (tab → split). Remembers the choice
// for the next rail open, then moves the live panel. moveTo detaches the panel
// from its old group, so the clear-on-close URL listener is held off the same
// way closeSubjectDeleted holds it — a move is not a close.
export function setTasksPlacement(placement: TasksPlacement): void {
  _sessionTasksPlacement = placement;
  writeTasksPlacement(placement);
  const tasks = _api?.getPanel(TASKS_PANEL_ID);
  const messages = _api?.getPanel(MESSAGES_PANEL_ID);
  if (!tasks || !messages) return;
  if (placement === "split" && !canSplitBesideMessages()) return;
  _shellOpDepth += 1;
  try {
    tasks.api.moveTo({ group: messages.group, position: placement === "tab" ? "center" : "right" });
  } finally {
    _shellOpDepth -= 1;
  }
  tasks.api.setActive();
  recomputeSnapshot();
}

export function openPanel(
  panelKey: string,
  params: Record<string, string> = {},
  opts: { pushUrl?: boolean; oneShots?: Record<string, string>; focus?: boolean } = {},
): void {
  const descriptor = describePanel(panelKey, params);
  if (!descriptor) return;
  if (descriptor.component === "chat") {
    openChatPanel(descriptor.params.agentId);
    return;
  }
  addOrFocus(descriptor, opts.oneShots);
  if (opts.pushUrl) {
    pushSearchParams(new URLSearchParams(buildPanelUrl(panelKey, params).split("?")[1]));
  }
}

// Every agent shortcut retargets the same Messages panel.
export function openChatPanel(agentId: string): void {
  const descriptor = describePanel("chat", { agent: agentId });
  if (descriptor && _api) {
    deferFocusComposerRequest(agentId);
    addOrFocus(descriptor);
  }
}

// Room permalinks (?panel=chat&room=…&message=…) retarget the same Messages
// panel with room/message params instead of an agent id. Mirrors
// openChatPanel's shape; kept separate rather than overloading openChatPanel's
// signature since that one is called from many sites with a bare agentId.
export function openMessagePanel(roomId: string, messageId?: string): void {
  const descriptor = describePanel("chat", { room: roomId, ...(messageId ? { message: messageId } : {}) });
  if (descriptor && _api) addOrFocus(descriptor);
}

let sessionIntent = 0;
/** Retargets the Messages panel at a workspace session; the session view itself
 *  lives inside Messages (spec §4), never as its own dock tab. */
export function openSessionPanel(sessionId: string): void {
  const descriptor = describePanel("chat", { session: sessionId });
  if (descriptor && _api) addOrFocus(descriptor, { sessionIntent: String(++sessionIntent) });
}

export function activateMessagesPanel(): void {
  const descriptor = describePanel("chat", { agent: DEFAULT_AGENT_SENTINEL });
  if (descriptor) addOrFocus({ ...descriptor, params: { agentSelection: "resolved" } });
}

// Metadata from the canonical room is not another agent-selection command.
// This marker is transient: layout sanitization persists only the agentId.
export function syncMessagesPanelAgent(agentId?: string): void {
  const panel = _api?.getPanel(MESSAGES_PANEL_ID);
  if (!panel || (panel.params?.agentId === agentId && panel.params?.agentSelection === "resolved")) return;
  panel.api.updateParameters({ agentId, agentSelection: "resolved" });
  recomputeSnapshot();
}

// Hand the default agent a drafted message. openChatPanel defers the slot so
// this immediate event cannot seed a same-agent composer in the wrong room;
// ChatPanel releases it once the selected DM is rendered. False when no
// default agent resolves (pre-provision) so callers can stay silent.
export function openChatPanelWithDraft(draft: string): boolean {
  const target = _resolveDefaultAgentId();
  if (!target) return false;
  requestFocusComposer(target, draft);
  openChatPanel(target);
  window.dispatchEvent(
    new CustomEvent("getstarted:focus-composer", { detail: { agentId: target } }),
  );
  return true;
}

// Sanctioned exception to "close never touches the URL" (spec §6 rule 8):
// same-tab delete of the URL-named detail. Order pinned: (a) replace URL to
// the list, (b) close self with the clear-on-close listener suppressed,
// (c) open-or-focus the list panel.
export function closeSubjectDeleted(selfPanelId: string, listPanelKey: string): void {
  if (!_api) return;
  const listUrl = buildPanelUrl(listPanelKey, {});
  replaceSearchParams(new URLSearchParams(listUrl.split("?")[1]));
  _shellOpDepth += 1;
  try {
    const self = _api.getPanel(selfPanelId);
    if (self) _api.removePanel(self);
    openPanel(listPanelKey);
  } finally {
    _shellOpDepth -= 1;
  }
}

export function isShellOperationInProgress(): boolean {
  return _shellOpDepth > 0;
}

// Reset = destroy and rebuild. clear() removes EVERY panel and group — no
// instance survives (a live chat panel is torn down with the rest) — then
// `build` constructs the code-built default layout from scratch. Runs under
// the shell-op guard so the clear-on-close URL listener ignores the mass
// removals.
export function resetWorkspaceLayout(build: (api: DockviewApi) => void): void {
  if (!_api) return;
  _shellOpDepth += 1;
  try {
    _api.clear();
    build(_api);
  } finally {
    _shellOpDepth -= 1;
  }
}

function subscribe(l: () => void): () => void {
  _listeners.add(l);
  return () => _listeners.delete(l);
}

export function useWorkspaceState(): WorkspaceSnapshot {
  // `subscribe` is a module-level function (stable identity across renders —
  // see useIsDesktop.ts for the same pattern), so no
  // useCallback wrapper is needed or wanted (react-hooks/exhaustive-deps
  // flags useCallback(namedFn, []) as suspicious).
  return useSyncExternalStore(
    subscribe,
    () => _snapshot,
    () => _snapshot,
  );
}

// The Browser panel's lower slot. While Browser and Messages are both on screen, the room's
// side column (a thread, its tasks, room details) renders there, under the live browser, so
// the room keeps its full width instead of being squeezed into a third column (owner ruling
// 2026-09-26: two columns, browser on top and thread below on the right).
let _threadHost: HTMLElement | null = null;
const _hostListeners = new Set<() => void>();

export function setBrowserThreadHost(element: HTMLElement | null): void {
  if (_threadHost === element) return;
  _threadHost = element;
  _hostListeners.forEach((l) => l());
}

function subscribeHost(l: () => void): () => void {
  _hostListeners.add(l);
  return () => _hostListeners.delete(l);
}

/** Where the room's side column renders instead of beside the room, or null to keep it inline. */
export function useBrowserThreadHost(): HTMLElement | null {
  const host = useSyncExternalStore(subscribeHost, () => _threadHost, () => null);
  const { browserVisible, messagesVisible } = useWorkspaceState();
  // Both the active tab of some group = side by side. A Browser tab behind Messages is not.
  return host && browserVisible && messagesVisible ? host : null;
}

// The Mac run the Browser panel shows instead of the cloud browser (null: the cloud one).
// The panel falls back to the cloud view on its own once that run is no longer running.
let _macRun: string | null = null;
const _macListeners = new Set<() => void>();

export function selectBrowserMacRun(runId: string | null): void {
  if (_macRun === runId) return;
  _macRun = runId;
  _macListeners.forEach((l) => l());
}

/** Show a run's Mac tab (read-only frames) in the Browser panel. False: no dock mounted. */
export function showMacRunInBrowser(runId: string): boolean {
  selectBrowserMacRun(runId);
  return showBrowserBesideChat({ focus: true, asTabWhenNarrow: true });
}

export function useBrowserMacRun(): string | null {
  return useSyncExternalStore(
    (l) => { _macListeners.add(l); return () => { _macListeners.delete(l); }; },
    () => _macRun,
    () => null,
  );
}

// Synchronous read of the live snapshot for non-React callers (the shell's
// URL reconciler checks panel newness for analytics at execute time, before
// any re-render would refresh a useWorkspaceState value).
export function getWorkspaceSnapshot(): WorkspaceSnapshot {
  return _snapshot;
}

export function _snapshotForTests(): WorkspaceSnapshot {
  return _snapshot;
}

export function _resetControllerForTests(): void {
  _api = null;
  _resolveDefaultAgentId = () => undefined;
  _shellOpDepth = 0;
  _snapshot = emptySnapshot();
  _listeners.clear();
  _threadHost = null;
  _hostListeners.clear();
  _macRun = null;
  _macListeners.clear();
  _sessionTasksPlacement = null;
}
