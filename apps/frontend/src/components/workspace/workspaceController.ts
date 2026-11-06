// Module-level singleton owning the workspace's two slots: the PLACE's main
// view (Chat, Activity, Work, Library) and the one right PANE (a thread, a file,
// the browser, an agent's profile, a task's detail, mail). The repo's store
// pattern (useIsDesktop): module state + Set of listeners + useSyncExternalStore.
// EVERY programmatic open goes through openPanel/openChatPanel — a toast or
// agent event that bypasses this API silently loses URL integration.
"use client";

import { useEffect, useId, useRef, useSyncExternalStore } from "react";
import { pushSearchParams, replaceSearchParams } from "@/lib/shellNav";
import { deferFocusComposerRequest, requestFocusComposer } from "@/lib/focusComposerRequest";
import {
  describePanel,
  MESSAGES_PANEL_ID,
  clearablePanelParamKeys,
  DEFAULT_AGENT_SENTINEL,
  opensInPane,
  type PanelDescriptor,
} from "./panelRegistry";
import { currentChatViewSearch } from "./chatHistory";
import { appendPane, buildPanelUrl, clearPanelFromUrl } from "./urlChannel";

export type Place = "home" | "chat" | "activity" | "work" | "team" | "library" | "canvas" | "settings";

/** What a slot shows. `thread` is the room's side column (a thread, its tasks, room details),
 *  portalled into the pane by the room itself (useThreadPane). */
export interface Slot {
  panelId: string;
  component: PanelDescriptor["component"] | "thread";
  title: string;
  params: Record<string, string>;
  /** The URL grammar this slot was opened with (key + raw params), so a pushed URL can name
   *  both slots. None for the thread pane, which has no URL of its own. */
  url?: { key: string; params: Record<string, string> };
}

export interface WorkspaceSnapshot {
  ready: boolean;
  place: Place;
  /** The place's main view; null for Chat, whose conversation is always mounted (`chatParams`). */
  main: Slot | null;
  chatParams: Record<string, string>;
  pane: Slot | null;
  /** The pane has the screen; a narrow chat column stays on its left. */
  fullScreen: boolean;
  /** The main view's panel id (Messages for Chat). */
  activePanelId: string | null;
  focusedChatAgentId: string | null; // sentinel RESOLVED when possible
  /** Sticky variant of focusedChatAgentId: the last chat agent that WAS on screen. */
  lastFocusedChatAgentId: string | null;
  visibleChatAgentIds: string[];
  teamsPanelVisible: boolean;
  /** The room is on screen: the Chat place, the full-screen pane's chat column, or a thread in the pane. */
  messagesVisible: boolean;
  browserVisible: boolean;
  openPanelIds: string[];
}

export const BROWSER_PANEL_ID = "browser";
export const THREAD_PANE_ID = "thread";

let _attached = false;
let _resolveDefaultAgentId: () => string | undefined = () => undefined;
let _main: Slot | null = null;
let _chatParams: Record<string, string> = { agentId: DEFAULT_AGENT_SENTINEL };
let _pane: Slot | null = null;
let _fullScreen = false;
let _snapshot: WorkspaceSnapshot = buildSnapshot(null);
const _listeners = new Set<() => void>();

export { opensInPane };

export function placeOf(main: Slot | null): Place {
  if (!main) return "chat";
  if (main.component === "home") return "home";
  if (main.component === "activity") return "activity";
  if (main.component === "canvas") return "canvas";
  if (main.component === "settings") return "settings";
  if (main.component === "teams" && main.params.teamsKey === "artifacts") return "library";
  if (main.component === "teams" && main.params.teamsKey === "agents") return "team";
  return "work";
}

function chatAgentId(): string | null {
  const raw = _chatParams.agentId;
  if (!raw) return null;
  if (raw === DEFAULT_AGENT_SENTINEL) return _resolveDefaultAgentId() ?? null;
  return raw;
}

function buildSnapshot(previous: WorkspaceSnapshot | null): WorkspaceSnapshot {
  const place = placeOf(_main);
  const fullScreen = _fullScreen && !!_pane;
  // The room is on screen in Chat, beside a full-screen pane, or as a thread in the pane from
  // any place: each needs the live room subscription and counts as a visible chat.
  const messagesVisible = _attached && (place === "chat" || fullScreen || _pane?.panelId === THREAD_PANE_ID);
  const agent = _attached ? chatAgentId() : null;
  return {
    ready: _attached,
    place,
    main: _main,
    chatParams: _chatParams,
    pane: _pane,
    fullScreen,
    activePanelId: _attached ? _main?.panelId ?? MESSAGES_PANEL_ID : null,
    focusedChatAgentId: place === "chat" ? agent : null,
    lastFocusedChatAgentId: (messagesVisible ? agent : null) ?? previous?.lastFocusedChatAgentId ?? null,
    visibleChatAgentIds: messagesVisible && agent ? [agent] : [],
    teamsPanelVisible: !!(_main?.panelId.startsWith("teams-") || _pane?.panelId.startsWith("teams-")),
    messagesVisible,
    browserVisible: _pane?.panelId === BROWSER_PANEL_ID,
    openPanelIds: _attached
      ? [MESSAGES_PANEL_ID, ...(_main ? [_main.panelId] : []), ...(_pane ? [_pane.panelId] : [])]
      : [],
  };
}

const sameIds = (a: readonly string[], b: readonly string[]) => a.length === b.length && a.every((id, i) => id === b[i]);

// Object identity IS the change signal (useSyncExternalStore compares with Object.is):
// keep the previous object when nothing moved, so no subscriber re-renders for nothing.
function commit(): void {
  const next = buildSnapshot(_snapshot);
  const prev = _snapshot;
  const same = (Object.keys(next) as (keyof WorkspaceSnapshot)[]).every((k) =>
    Array.isArray(next[k]) ? sameIds(next[k] as string[], prev[k] as string[]) : next[k] === prev[k]);
  if (same) return;
  _snapshot = next;
  _listeners.forEach((l) => l());
}

/** The shell mounts: the slots exist from now on. Returns the detach. */
export function attachShell(resolveDefaultAgentId: () => string | undefined): () => void {
  _attached = true;
  _resolveDefaultAgentId = resolveDefaultAgentId;
  commit();
  return () => {
    _attached = false;
    _resolveDefaultAgentId = () => undefined;
    _main = null;
    _chatParams = { agentId: DEFAULT_AGENT_SENTINEL };
    _pane = null;
    _fullScreen = false;
    commit();
  };
}

// The roster arriving re-resolves the default-agent sentinel; nothing else would.
export function refreshWorkspaceSnapshot(): void {
  commit();
}

/** A re-open re-delivers the descriptor's durable params and any one-shots, and clears a
 *  stale clearable param the fresh open does not carry (plain nav drops old filters). */
function nextParams(current: Slot | null, descriptor: PanelDescriptor, extra?: Record<string, string>): Record<string, string> {
  const fresh = { ...descriptor.params, ...extra };
  if (current?.panelId !== descriptor.panelId) return fresh;
  const merged = { ...current.params, ...fresh };
  for (const k of clearablePanelParamKeys(descriptor.panelId)) if (!(k in fresh)) delete merged[k];
  return merged;
}

/** A pane item the URL named is gone from the pane: clear it in place, so a refresh can't reopen it. */
function clearPaneFromUrl(previous: Slot | null): void {
  if (!previous) return;
  const cleared = clearPanelFromUrl(window.location.search, previous.panelId);
  if (cleared !== null) replaceSearchParams(new URLSearchParams(cleared));
}

function show(descriptor: PanelDescriptor, extra?: Record<string, string>, clearReplacedPane = false,
  url?: Slot["url"]): void {
  if (!_attached) return;
  if (descriptor.component === "chat") {
    _chatParams = nextParams({ panelId: MESSAGES_PANEL_ID, component: "chat", title: "", params: _chatParams }, descriptor, extra);
    _main = null;
    _fullScreen = false;
  } else if (opensInPane(descriptor)) {
    // One thing at a time: whatever the pane showed is replaced (and leaves the URL, unless
    // this open writes its own).
    if (clearReplacedPane && _pane && _pane.panelId !== descriptor.panelId) clearPaneFromUrl(_pane);
    _pane = { panelId: descriptor.panelId, component: descriptor.component, title: descriptor.title, params: nextParams(_pane, descriptor, extra), url };
  } else {
    _main = { panelId: descriptor.panelId, component: descriptor.component, title: descriptor.title, params: nextParams(_main, descriptor, extra), url };
    // Picking a place shows it: a full-screen pane folds back beside it.
    _fullScreen = false;
  }
  commit();
}

export function openPanel(
  panelKey: string,
  params: Record<string, string> = {},
  opts: { pushUrl?: boolean; oneShots?: Record<string, string> } = {},
): void {
  const descriptor = describePanel(panelKey, params);
  if (!descriptor) return;
  if (descriptor.component === "chat" && descriptor.params.agentId) openChatPanel(descriptor.params.agentId);
  else show(descriptor, opts.oneShots, !opts.pushUrl, { key: panelKey, params: { ...params } });
  if (opts.pushUrl) {
    pushSearchParams(descriptor.component === "chat" ? new URLSearchParams(buildPanelUrl(panelKey, params).split("?")[1]) : slotsUrl());
  }
}

/** The URL for what is on screen: the place, plus the pane beside it (Chat as the place: the
 *  pane alone, the plain `panel=` form). A reload or a copied link restores both. */
function slotsUrl(): URLSearchParams {
  // With Chat as the place, the place is the room or session on screen (?panel=chat&room=…),
  // so a reload restores the conversation beside the pane, not the default one.
  const chat = !_main && _pane?.url ? currentChatViewSearch() : null;
  if (chat && _pane?.url) {
    const sp = new URLSearchParams(chat);
    appendPane(sp, _pane.url.key, _pane.url.params);
    return sp;
  }
  const primary = _main?.url ?? _pane?.url;
  const sp = new URLSearchParams(primary ? buildPanelUrl(primary.key, primary.params).split("?")[1] : "");
  if (_main?.url && _pane?.url) appendPane(sp, _pane.url.key, _pane.url.params);
  return sp;
}

/** Merge a panel's own param change (a one-shot consumed, a filter dismissed); `undefined` deletes. */
export function updatePanelParams(panelId: string, partial: Record<string, string | undefined>): void {
  const apply = (params: Record<string, string>) => {
    const merged: Record<string, string | undefined> = { ...params, ...partial };
    for (const k of Object.keys(partial)) if (partial[k] === undefined) delete merged[k];
    return merged as Record<string, string>;
  };
  // A dismissed durable param also leaves the slot's URL grammar, so the next pushed URL
  // does not bring it back (same-named keys: agent, groupBy, item, path, …).
  const dropFromUrl = (url: Slot["url"]) => {
    if (!url) return url;
    const params = { ...url.params };
    for (const k of Object.keys(partial)) if (partial[k] === undefined) delete params[k];
    return { ...url, params };
  };
  if (panelId === MESSAGES_PANEL_ID) _chatParams = apply(_chatParams);
  else if (_main?.panelId === panelId) _main = { ..._main, params: apply(_main.params), url: dropFromUrl(_main.url) };
  else if (_pane?.panelId === panelId) _pane = { ..._pane, params: apply(_pane.params), url: dropFromUrl(_pane.url) };
  else return;
  commit();
}

function dropPane(): void {
  _pane = null;
  _fullScreen = false;
  commit();
}

/** The pane's close: a URL that named what it showed is cleared in place, so refresh can't reopen it. */
export function closePane(): void {
  if (!_pane) return;
  clearPaneFromUrl(_pane);
  dropPane();
}

export function setPaneFullScreen(full: boolean): void {
  if (_fullScreen === full || (full && !_pane)) return;
  _fullScreen = full;
  commit();
}

// Every agent shortcut retargets the same conversation.
export function openChatPanel(agentId: string): void {
  const descriptor = describePanel("chat", { agent: agentId });
  if (descriptor && _attached) {
    deferFocusComposerRequest(agentId);
    show(descriptor);
  }
}

// Room permalinks (?panel=chat&room=…&message=…) retarget the conversation at a room.
export function openMessagePanel(roomId: string, messageId?: string): void {
  const descriptor = describePanel("chat", { room: roomId, ...(messageId ? { message: messageId } : {}) });
  if (descriptor) show(descriptor);
}

let sessionIntent = 0;
/** Retargets the conversation at a workspace session (the session view lives inside Chat);
 *  `seq` opens its conversation at that turn. */
/** A super-agent thread in the right pane (`?panel=task-detail&taskId=`). */
export function openTaskPane(taskId: string): void {
  openPanel("task-detail", { taskId }, { pushUrl: true });
}

export function openSessionPanel(sessionId: string, seq?: number): void {
  const descriptor = describePanel("chat", { session: sessionId, ...(seq ? { seq: String(seq) } : {}) });
  if (descriptor) show(descriptor, { sessionIntent: String(++sessionIntent) });
}

export function activateMessagesPanel(): void {
  const descriptor = describePanel("chat", { agent: DEFAULT_AGENT_SENTINEL });
  if (descriptor) show({ ...descriptor, params: { agentSelection: "resolved" } });
}

// Metadata from the canonical room is not another agent-selection command.
export function syncMessagesPanelAgent(agentId?: string): void {
  if (!_attached || (_chatParams.agentId === agentId && _chatParams.agentSelection === "resolved")) return;
  updatePanelParams(MESSAGES_PANEL_ID, { agentId, agentSelection: "resolved" });
}

// Hand the default agent a drafted message. False when no default agent
// resolves (pre-provision) so callers can stay silent.
export function openChatPanelWithDraft(draft: string): boolean {
  const target = _resolveDefaultAgentId();
  if (!target) return false;
  openAgentDmWithDraft(target, draft);
  return true;
}

/** One agent's DM with the composer focused; a non-empty draft is typed in first. */
export function openAgentDmWithDraft(agentId: string, draft: string): void {
  if (draft) requestFocusComposer(agentId, draft);
  openChatPanel(agentId);
  window.dispatchEvent(new CustomEvent("getstarted:focus-composer", { detail: { agentId } }));
}

// Same-place delete of the URL-named detail. Order pinned: (a) replace URL to
// the list, (b) close the detail (no clear-on-close: the URL already moved),
// (c) open the list.
export function closeSubjectDeleted(selfPanelId: string, listPanelKey: string): void {
  if (!_attached) return;
  const listUrl = buildPanelUrl(listPanelKey, {});
  replaceSearchParams(new URLSearchParams(listUrl.split("?")[1]));
  if (_pane?.panelId === selfPanelId) dropPane();
  openPanel(listPanelKey);
}

function subscribe(l: () => void): () => void {
  _listeners.add(l);
  return () => _listeners.delete(l);
}

export function useWorkspaceState(): WorkspaceSnapshot {
  return useSyncExternalStore(subscribe, () => _snapshot, () => _snapshot);
}

// The thread pane's slot (RightPane renders it while the pane shows a thread).
let _threadHost: HTMLElement | null = null;
const _hostListeners = new Set<() => void>();

export function setThreadPaneHost(element: HTMLElement | null): void {
  if (_threadHost === element) return;
  _threadHost = element;
  _hostListeners.forEach((l) => l());
}

function subscribeHost(l: () => void): () => void {
  _hostListeners.add(l);
  return () => _hostListeners.delete(l);
}

const ownsPane = (owner: string) => _pane?.panelId === THREAD_PANE_ID && _pane.params.owner === owner;

/**
 * The room's side column (a thread, its tasks, room details) in the right pane. While `open`,
 * the room claims the pane; when something else takes the pane (or its close button), the
 * room folds the column with `close`. Returns where to portal the column, or null to keep it
 * inline (no shell mounted, or the pane not the room's yet).
 */
export function useThreadPane(open: boolean, close: () => void, title: string, item = false): HTMLElement | null {
  const owner = useId();
  const { ready, pane } = useWorkspaceState();
  const host = useSyncExternalStore(subscribeHost, () => _threadHost, () => null);
  const closeRef = useRef(close);
  useEffect(() => { closeRef.current = close; });
  const mine = pane?.panelId === THREAD_PANE_ID && pane.params.owner === owner;
  // Claims on opening only: while open, something else may have taken the pane (the column
  // is folding), and a title change then must not take it back.
  const wasOpen = useRef(false);
  useEffect(() => {
    if (!ready) return;
    const opening = open && !wasOpen.current;
    wasOpen.current = open;
    if (open && (opening || ownsPane(owner))) {
      // `item`: one task (or thread) is open in the column, so the pane takes the item width (RightPane).
      if (ownsPane(owner) && _pane!.title === title && (_pane!.params.item === "1") === item) return;
      if (!ownsPane(owner)) clearPaneFromUrl(_pane);
      _pane = { panelId: THREAD_PANE_ID, component: "thread", title, params: item ? { owner, item: "1" } : { owner } };
      commit();
    } else if (!open && ownsPane(owner)) {
      dropPane();
    }
  }, [open, ready, owner, title, item]);
  useEffect(() => () => { if (ownsPane(owner)) dropPane(); }, [owner]);
  const wasMine = useRef(false);
  useEffect(() => {
    if (wasMine.current && !mine && open) closeRef.current();
    wasMine.current = mine;
  }, [mine, open]);
  return mine ? host : null;
}

/** Show the shared Browser in the pane. False when no shell is mounted (the caller falls back). */
export function openBrowserPane(): boolean {
  if (!_attached) return false;
  // The URL names what the pane shows, so a refresh reopens the Browser.
  openPanel(BROWSER_PANEL_ID, {}, { pushUrl: true });
  return true;
}

// The Mac run the Browser pane shows instead of the cloud browser (null: the cloud one).
let _macRun: string | null = null;
const _macListeners = new Set<() => void>();

export function selectBrowserMacRun(runId: string | null): void {
  if (_macRun === runId) return;
  _macRun = runId;
  _macListeners.forEach((l) => l());
}

/** Show a run's Mac tab (read-only frames) in the Browser pane. False: no shell mounted. */
export function showMacRunInBrowser(runId: string): boolean {
  selectBrowserMacRun(runId);
  return openBrowserPane();
}

export function useBrowserMacRun(): string | null {
  return useSyncExternalStore(
    (l) => { _macListeners.add(l); return () => { _macListeners.delete(l); }; },
    () => _macRun,
    () => null,
  );
}

// The Invite people dialog (InvitePeopleDialog, mounted once by WorkspaceShell).
let _inviteOpen = false;
const _inviteListeners = new Set<() => void>();

function setInviteDialog(open: boolean): void {
  if (_inviteOpen === open) return;
  _inviteOpen = open;
  _inviteListeners.forEach((l) => l());
}

export function openInviteDialog(): void { setInviteDialog(true); }
export function closeInviteDialog(): void { setInviteDialog(false); }

export function useInviteDialogOpen(): boolean {
  return useSyncExternalStore(
    (l) => { _inviteListeners.add(l); return () => { _inviteListeners.delete(l); }; },
    () => _inviteOpen,
    () => false,
  );
}

// Synchronous read of the live snapshot for non-React callers.
export function getWorkspaceSnapshot(): WorkspaceSnapshot {
  return _snapshot;
}

export function _resetControllerForTests(): void {
  _attached = false;
  _resolveDefaultAgentId = () => undefined;
  _main = null;
  _chatParams = { agentId: DEFAULT_AGENT_SENTINEL };
  _pane = null;
  _fullScreen = false;
  _snapshot = buildSnapshot(null);
  _listeners.clear();
  _threadHost = null;
  _hostListeners.clear();
  _macRun = null;
  _macListeners.clear();
  _inviteOpen = false;
  _inviteListeners.clear();
}
