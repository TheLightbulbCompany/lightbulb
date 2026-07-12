// Module-level singleton owning the DockviewApi. The repo's store pattern
// (useActiveSession): module state + Set of listeners + useSyncExternalStore.
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
import {
  describePanel,
  chatPanelId,
  DEFAULT_AGENT_SENTINEL,
  type PanelDescriptor,
} from "./panelRegistry";
import { buildPanelUrl } from "./urlChannel";

export interface WorkspaceSnapshot {
  ready: boolean;
  activePanelId: string | null;
  focusedChatAgentId: string | null; // sentinel RESOLVED when possible
  visibleChatAgentIds: string[]; // one entry per dockview group whose active panel is a chat panel
  teamsPanelVisible: boolean;
  openPanelIds: string[];
}

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
    visibleChatAgentIds: [],
    teamsPanelVisible: false,
    openPanelIds: [],
  };
}

function notify(): void {
  _listeners.forEach((l) => l());
}

function agentIdOfChatPanel(panel: IDockviewPanel | undefined): string | null {
  if (!panel || !panel.id.startsWith("chat:")) return null;
  const raw = panel.id.slice("chat:".length);
  if (raw === DEFAULT_AGENT_SENTINEL) return _resolveDefaultAgentId() ?? null;
  return raw;
}

function recomputeSnapshot(): void {
  if (!_api) {
    _snapshot = emptySnapshot();
    notify();
    return;
  }
  const active = _api.activePanel;
  const openPanelIds = _api.panels.map((p) => p.id);
  const groupActivePanels = _api.groups.map((g) => g.activePanel);
  // Dedupe: a restored layout can hold both a sentinel chat panel
  // (chat:__default__) and the concrete chat panel it resolves to in separate
  // groups — both map to the same agentId, and a duplicate would wrongly widen
  // the desktop suppression key set (harmless there, but the snapshot is a
  // shared contract, so keep it clean).
  const visibleChatAgentIds = Array.from(
    new Set(
      groupActivePanels
        .map((p) => agentIdOfChatPanel(p))
        .filter((id): id is string => id !== null),
    ),
  );
  _snapshot = {
    ready: true,
    activePanelId: active?.id ?? null,
    focusedChatAgentId: agentIdOfChatPanel(active),
    visibleChatAgentIds,
    // Visible = the ACTIVE tab of some group (same traversal as
    // visibleChatAgentIds), NOT merely open: Mission Control keeps teams
    // panels permanently open, so open-based suppression would swallow every
    // teams run toast on the default layout.
    teamsPanelVisible: groupActivePanels.some(
      (p) => p !== undefined && p.id.startsWith("teams-"),
    ),
    openPanelIds,
  };
  notify();
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
  recomputeSnapshot();
  return () => {
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
    // updateParameters merges, so params absent from this open are untouched.
    const nextParams = { ...descriptor.params, ...extraParams };
    if (Object.keys(nextParams).length > 0) {
      existing.api.updateParameters(nextParams);
    }
    existing.api.setActive();
    return;
  }
  _api.addPanel({
    id: descriptor.panelId,
    component: descriptor.component,
    title: descriptor.title,
    params: { ...descriptor.params, ...extraParams },
    ...(descriptor.renderer ? { renderer: descriptor.renderer } : {}),
  });
}

export function openPanel(
  panelKey: string,
  params: Record<string, string> = {},
  opts: { pushUrl?: boolean; oneShots?: Record<string, string>; focus?: boolean } = {},
): void {
  const descriptor = describePanel(panelKey, params);
  if (!descriptor) return;
  if (descriptor.component === "chat") {
    // Chat identity needs sentinel unification — delegate.
    openChatPanel(descriptor.params.agentId);
    return;
  }
  addOrFocus(descriptor, opts.oneShots);
  if (opts.pushUrl) {
    pushSearchParams(new URLSearchParams(buildPanelUrl(panelKey, params).split("?")[1]));
  }
}

// One chat panel per agent. A sentinel panel (`chat:__default__`, from the
// Mission Control default layout) durably means "the CEO/default agent" —
// opening the concrete agent it currently resolves to must focus the
// sentinel panel, not open a duplicate.
export function openChatPanel(agentId: string): void {
  if (!_api) return;
  const concrete = _api.getPanel(chatPanelId(agentId));
  if (concrete) {
    concrete.api.setActive();
    return;
  }
  if (agentId !== DEFAULT_AGENT_SENTINEL) {
    const sentinel = _api.getPanel(chatPanelId(DEFAULT_AGENT_SENTINEL));
    if (sentinel && _resolveDefaultAgentId() === agentId) {
      sentinel.api.setActive();
      return;
    }
  } else {
    const resolved = _resolveDefaultAgentId();
    if (resolved) {
      const concreteDefault = _api.getPanel(chatPanelId(resolved));
      if (concreteDefault) {
        concreteDefault.api.setActive();
        return;
      }
    }
  }
  const descriptor = describePanel("chat", { agent: agentId });
  if (descriptor) addOrFocus(descriptor);
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

// Sidebar agent-delete: close the agent's chat panel if it happens to be
// open. Removed under the shell-op guard so the clear-on-close listener
// doesn't touch the URL mid-delete; a bare ?agent still naming the deleted
// agent is left for the reconciler, which resolves it to the agent-gone
// state.
export function closeChatPanelIfOpen(agentId: string): void {
  if (!_api) return;
  const panel = _api.getPanel(chatPanelId(agentId));
  if (!panel) return;
  _shellOpDepth += 1;
  try {
    _api.removePanel(panel);
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
  // see useIsDesktop.ts / useActiveSession.ts for the same pattern), so no
  // useCallback wrapper is needed or wanted (react-hooks/exhaustive-deps
  // flags useCallback(namedFn, []) as suspicious).
  return useSyncExternalStore(
    subscribe,
    () => _snapshot,
    () => _snapshot,
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
}
