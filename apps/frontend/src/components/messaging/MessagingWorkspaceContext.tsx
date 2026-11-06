"use client";

import { createContext, useContext, useCallback, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { useAuth, useUser } from "@clerk/nextjs";
import { canChangeListenMode, cardReplyRequest, markSessionRead, markSessionsSeen, markSessionUnread, resolveTypedMentions, roomDisplayName, runView, sessionArchived, sessionMentionCandidates, sessionUnread, useWorkspaceSessions, workspaceSections, type CardReply, type CreateMessageRequest, type ListenMode, type MemberRef, type RoomMember, type SessionSource, type RoomSummary } from "@lightbulb/shared";
import { useRooms } from "@/hooks/useRooms";
import { useCopyJoinLink } from "@/hooks/useCopyJoinLink";
import { useBilling } from "@/hooks/useBilling";
import { useAgents } from "@/hooks/useAgents";
import { useRosterEntries } from "@/components/agents/useRosterEntries";
import { useTeamsApi } from "@/hooks/useTeamsApi";
import { useChatReady } from "@/components/chat/shell/useChatReady";
import type { CompanyAgent, CompanyMember } from "@/components/teams/shared/types";
import { resolveCeoAgentId } from "@/components/getstarted/getStartedFirstTask";
import { useSuperAgent } from "@/hooks/useLiveEvents";
import { activateMessagesPanel, getWorkspaceSnapshot, openPanel, useWorkspaceState, type WorkspaceSnapshot } from "@/components/workspace/workspaceController";
import { placeTabTitle } from "@/components/workspace/tabTitle";
import { parseCommand } from "@/components/workspace/urlChannel";
import { MessageNotifications } from "@/components/notifications/MessageNotifications";
import { NeedsYouTitleBadge, UnreadTitleBadge } from "@/components/notifications/UnreadTitleBadge";
import { useSessionNotificationPrefs } from "@/components/settings/SessionNotificationsCard";
import { hasBlockingShortcutDialog } from "@/components/teams/shared/lib/keyboardShortcuts";
import { capture } from "@/lib/analytics";
import { clearDraft, draftKey, listDraftRoomIds, readDraft, writeDraft } from "./drafts";
import { adjacentRoomId, nextUnreadRoomId } from "./roomNavigation";
import { usePresence } from "./usePresence";
import { readSessionSeen, writeSessionSeen } from "./sidebarPrefs";
import { ShortcutSheet } from "./ShortcutSheet";
import type { RoomComposerProps } from "./RoomComposer";
import type { RoomDirectoryProps, ConversationCreation } from "./RoomDirectory";
import type { RoomPanelProps } from "./RoomPanel";
import type { SessionFocusTarget } from "./SessionFocus";
import { bridge } from "./localTerminal";
import type { SidebarRow, SidebarSession } from "./roomSections";
import { ownWaitingCount } from "./sessionGroups";
import { useSessionRows } from "./useSessionRows";
import type { SessionRow } from "@lightbulb/shared";
import type { DirectoryRoom, DirectoryParticipant, RoomParticipant } from "./types";


/** What the front agent is called while the super-agent switch is on (contracts-v2 §9). */
const FRONT_AGENT_NAME = "Lightbulb";
/** The starter front agent's roster (seat) name: shown as Lightbulb while the roster is still loading. */
const SEAT_NAME = "CEO";
const FRONT_KEY = "lightbulb.frontAgentId";
export function readFrontAgentId(scope: string): string | undefined {
  try { return localStorage.getItem(`${FRONT_KEY}:${scope}`) ?? undefined; } catch { return undefined; }
}
function writeFrontAgentId(scope: string, id: string): void {
  try { localStorage.setItem(`${FRONT_KEY}:${scope}`, id); } catch { /* this load only */ }
}
export interface MessagingWorkspaceValue {
  activeRoomId: string | null;
  directoryProps: RoomDirectoryProps;
  roomProps: RoomPanelProps | null;
  /** `explicit` false only for the default-agent fallback, which isn't a user's pick. */
  selectAgentDm: (agentId: string, explicit?: boolean) => Promise<string | null>;
  openMessage: (roomId: string, messageId: string | null) => void;
  openNotificationTarget: (roomId: string, threadId: string | null, messageId?: string) => void;
  /** `seq`: open the session's conversation at that turn. */
  openSession: (sessionId: string, chat?: boolean, seq?: number) => void;
  /** Open or close the focused session's chat column. */
  setSessionChat: (open: boolean) => void;
  /** The chat room on screen beside a session (SessionChat reports it), or null. */
  setSessionChatRoomId: (roomId: string | null) => void;
  /** A side column (an app's conversation in Canvas) says which room it shows under its own key, or null when hidden.
   * Each one is suppressed from notifications on its own, beside the main room. */
  setSideRoom: (key: string, roomId: string | null) => void;
  /** Focus an empty new session (the composer that starts one on your Mac). */
  openNewSession: () => void;
  sessionFocus: SessionFocusTarget | null;
  closeSession: () => void;
  /** Not gated on the active dock panel: a split dock keeps the focused session on screen. */
  sessionSource: SessionSource;
  /** ownerName/own for a session fetched directly, outside the workspace session list. */
  resolveSessionOwner: (humanUserId: string) => { ownerName: string; own: boolean };
  workspaceSessions: ReturnType<typeof useWorkspaceSessions>;
  scope: string;
  /** Latched at the room list's first load per workspace; null until then. */
  freshLanding: boolean | null;
  /** Counts a person's picks; the desktop shell records a view in history only when it moved. */
  explicitPickCount: number;
  /** Super-agent Home's "Ask Lightbulb…": sends the text into the Lightbulb DM, then opens it. Rejects when it
   *  could not send (no front agent yet, the DM or the send failed). */
  askLightbulb: (text: string) => Promise<void>;
}

/** Super-agent workspaces land on Home (founder, 6 Nov 2026), except a brand-new one, whose onboarding runs in
 *  #general. Only a bare landing moves: not a URL that names a place, a pane already open, or a pick already made. */
export function landsOnHome({ superAgent, fresh, search, snapshot, picked }: {
  superAgent: boolean; fresh: boolean | null; search: string; snapshot: Pick<WorkspaceSnapshot, "ready" | "place" | "pane">; picked: boolean;
}): boolean {
  return superAgent && fresh === false && snapshot.ready && snapshot.place === "chat" && !snapshot.pane && !picked
    && parseCommand(search) === null;
}

// A pick is "current" while neither the URL nor the focused dock tab moved:
// tab focus is URL-silent, so href alone misses a click on an open tab.
export interface PickToken { href: string; activePanelId: string | null | undefined }
export const pickToken = (): PickToken => ({
  href: window.location.href,
  activePanelId: getWorkspaceSnapshot()?.activePanelId,
});
export const pickTokenUnchanged = (a: PickToken, b: PickToken) =>
  a.href === b.href && a.activePanelId === b.activePanelId;

/** A session owner as a human participant, the shape ParticipantAvatar takes. */
/** The native Share command; only a desktop page has engine rows, so only a desktop page reaches it. */
const shareEngineSession = (row: SessionRow): Promise<unknown> => Promise.resolve(bridge()?.("share_engine_session", { sessionId: row.id }));

function ownerFace(id: string, displayName: string): RoomParticipant {
  return { kind: "human", id, displayName, mentionHandle: null, role: "member", listenMode: null, joinedAt: "", joinedSeq: 0 };
}

/** Nothing but #general and the vendor shared room: a new workspace, whose home is #general. */
export function isFreshWorkspace(rooms: readonly RoomSummary[]): boolean {
  return rooms.every((room) => room.isDefault || room.shared);
}

/**
 * Where a default-agent request lands, only once it is certain: any other room
 * means returning (false); #general with nothing but the shared room beside it
 * means fresh (true); [] or shared-only means #general is not seeded yet (null).
 */
function landingFor(rooms: readonly RoomSummary[]): boolean | null {
  if (!isFreshWorkspace(rooms)) return false;
  return rooms.some((room) => room.isDefault) ? true : null;
}

// ponytail: fixed wait — provisioning seeds #general in ~15–50 s; a workspace
// that never gets one (legacy, #general left) opens the default DM a minute late.
// Make it event-driven if the backend ever says "seeded" explicitly.
const FRESH_LANDING_WAIT_MS = 60_000;

// Activation is a workspace milestone, not a count of every successful run.
// Keep the browser marker workspace-scoped so a remount or another room cannot
// report the same workspace twice. The event itself contains no message, room,
// run, or agent identifiers: only the stable aggregate join key.
const CHAT_COMPLETED_STORAGE_PREFIX = "isol8_chat_completed_v1";

const MessagingWorkspaceContext = createContext<MessagingWorkspaceValue | null>(null);

export function useMessagingWorkspace(): MessagingWorkspaceValue {
  const value = useContext(MessagingWorkspaceContext);
  if (!value) throw new Error("useMessagingWorkspace must be used within MessagingWorkspaceProvider");
  return value;
}

/** Null outside a MessagingWorkspaceProvider — the Teams command palette is
 *  also mounted in InlineTeamsPanel, where no messaging provider exists. */
export function useMessagingWorkspaceOptional(): MessagingWorkspaceValue | null {
  return useContext(MessagingWorkspaceContext);
}

/**
 * Window-level shortcuts for room navigation (⌥↑/⌥↓ adjacent room, ⌥⇧↑/⌥⇧↓
 * next/previous unread — both wrapping via roomNavigation.ts, the same rule
 * RoomDirectory's "next unread" button uses) and the shortcut sheet (⌘/,
 * toggled via `onShowShortcuts`).
 *
 * The room-navigation (⌥) branches are ignored while a Radix dialog/menu is
 * open or an IME composition is in progress. They deliberately still fire
 * from inside the composer — that is where the owner's hands are, and the
 * combos are modified, so they are not what anyone types into a message.
 * `defaultPrevented` covers everything the composer or a typeahead already
 * handled (e.g. its own plain-ArrowUp "edit last message"). `⌘/` carries its
 * own dialog guard rather than the shared one below: it must still toggle
 * the shortcut sheet closed while that very sheet (its own `role="dialog"`)
 * is open, but must not stack a sheet over someone else's dialog — the ⌘K
 * palette.
 */
function useMessagingShortcuts(
  rooms: readonly RoomSummary[],
  activeRoomId: string | null,
  selectRoom: (roomId: string) => void,
  onShowShortcuts: () => void,
  shortcutsOpen: boolean,
): void {
  useEffect(() => {
    const handler = (event: KeyboardEvent) => {
      if (event.defaultPrevented || event.isComposing || event.keyCode === 229) return;
      if ((event.metaKey || event.ctrlKey) && !event.altKey && !event.shiftKey && event.key === "/") {
        if (!shortcutsOpen && hasBlockingShortcutDialog()) return;
        event.preventDefault();
        onShowShortcuts();
        return;
      }
      if (hasBlockingShortcutDialog() || document.querySelector("[role='menu']")) return;
      if (!event.altKey || event.metaKey || event.ctrlKey) return;
      if (event.key !== "ArrowUp" && event.key !== "ArrowDown") return;
      const direction = event.key === "ArrowDown" ? 1 : -1;
      // The order the sidebar shows (sections, the chosen sort, collapsed groups), which
      // only RoomDirectory knows; the server's order when no sidebar is mounted.
      const shown = [...document.querySelectorAll<HTMLElement>(".room-directory [data-room-id]")]
        .flatMap((row) => rooms.find((room) => room.roomId === row.dataset.roomId) ?? []);
      const ordered = shown.length > 0 ? shown : rooms;
      const next = event.shiftKey
        ? nextUnreadRoomId(ordered, activeRoomId, direction)
        : adjacentRoomId(ordered, activeRoomId, direction);
      // Also a no-op when the only unread room is the one already open —
      // adjacentRoomId already excludes this case itself, but
      // nextUnreadRoomId (unlike RoomDirectory's own "next unread" button)
      // must not reselect the active room from this window-level shortcut.
      if (!next || next === activeRoomId) return;
      event.preventDefault();
      selectRoom(next);
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [rooms, activeRoomId, selectRoom, onShowShortcuts, shortcutsOpen]);
}

export function MessagingWorkspaceProvider({
  children,
  initialAgentId,
  defaultAgentId,
  subscribeToRoom = true,
  observeRuns = false,
}: {
  children: ReactNode;
  initialAgentId?: string | null;
  /** Opened only when nothing explicit was asked for and the workspace is not fresh. */
  defaultAgentId?: string | null;
  subscribeToRoom?: boolean;
  observeRuns?: boolean;
}) {
  const { userId, orgId } = useAuth();
  const scope = orgId ?? userId ?? "signed-out";
  // A room's Add people lists workspace members only, which is where people looked for "invite my
  // cofounder" and found nothing (Second Catch, 2026-10-08); the room panel offers the join link too.
  const copyJoinLinkFor = useCopyJoinLink();
  const copyJoinLink = useCallback(() => copyJoinLinkFor("room_members"), [copyJoinLinkFor]);
  // ⋯ → Add people on a sidebar row: open that room, then its member picker (one-shot).
  const [addPeopleRoomId, setAddPeopleRoomId] = useState<string | null>(null);
  // Focus placement: a session opened from the sidebar or a link takes the
  // middle of the panel; sessions live in the workspace, not in a room.
  const [sessionFocus, setSessionFocus] = useState<SessionFocusTarget | null>(null);
  // Focus covers the room. A covered room must neither auto-read nor silence its notifications.
  const roomVisible = subscribeToRoom && !sessionFocus;
  // The live provider carries ONE room channel. While a session's chat is open the covered room
  // lends it that channel, and catches up when it gets it back.
  const sessionChatOpen = !!sessionFocus?.chat;
  // Side columns on screen, by key: each room is kept from toasting on its own, the main room too.
  const [sideRooms, setSideRooms] = useState<Readonly<Record<string, string>>>({});
  const setSideRoom = useCallback((key: string, roomId: string | null) => setSideRooms((now) => {
    if ((now[key] ?? null) === roomId) return now;
    const next = { ...now };
    if (roomId) next[key] = roomId; else delete next[key];
    return next;
  }), []);
  const setSessionChatRoomId = useCallback((roomId: string | null) => setSideRoom("session-chat", roomId), [setSideRoom]);
  const sideRoomIds = useMemo(() => [...new Set(Object.values(sideRooms))], [sideRooms]);
  // Browser watches run frames even when the room is hidden; visibility still
  // controls read receipts and notification suppression.
  // Fresh or returning is decided once per workspace, as soon as the room list
  // makes it certain: a DM created later must not turn a new workspace
  // "returning" and pull the viewer into the default agent's DM mid-onboarding.
  const [landing, setLanding] = useState<{ scope: string; fresh: boolean } | null>(null);
  const freshLanding = landing?.scope === scope ? landing.fresh : null;
  const rooms = useRooms(
    defaultAgentId && freshLanding === false ? initialAgentId ?? defaultAgentId : initialAgentId,
    (subscribeToRoom || observeRuns) && !sessionChatOpen,
    roomVisible,
    true,
    true, // this instance feeds RoomPanel's subagent strip
  );
  const activeRunIds = useRef(new Set<string>());
  const completedRunIds = useRef(new Set<string>());
  useEffect(() => {
    // A replayed completed run is not a browser-observed completion. Require
    // this mounted client to have seen the run working before it completed.
    for (const run of rooms.runs) {
      if (run.state === "running") activeRunIds.current.add(run.runId);
      if (run.state === "completed" && activeRunIds.current.has(run.runId)) {
        completedRunIds.current.add(run.runId);
      }
    }

    if (!orgId || !userId || freshLanding !== true || completedRunIds.current.size === 0) return;
    const storageKey = `${CHAT_COMPLETED_STORAGE_PREFIX}:${orgId}`;
    if (window.localStorage.getItem(storageKey)) return;

    // A qualifying activation is the first real message this user sent in the
    // fresh workspace followed by its successful agent run. System-authored,
    // agent-authored, errored, aborted, and unrelated runs do not qualify.
    const ownMessages = rooms.messages.filter((message) =>
      message.author.kind === "human" && message.author.id === userId,
    );
    if (ownMessages.length !== 1) return;
    const firstMessageId = ownMessages[0].messageId;
    const completedFirstChat = rooms.runs.some((run) =>
      completedRunIds.current.has(run.runId) &&
      run.state === "completed" &&
      run.triggerMessageId === firstMessageId,
    );
    if (!completedFirstChat) return;

    // Persist before capture so React remounts and effect replays cannot emit a
    // duplicate. capture() is browser-only and deliberately receives no chat
    // content or per-run identifiers.
    window.localStorage.setItem(storageKey, "1");
    capture("chat_completed", { workspace_id: orgId });
  }, [freshLanding, orgId, rooms.messages, rooms.runs, userId]);
  const observedLanding = rooms.roomsLoading || rooms.roomsError ? null : landingFor(rooms.rooms);
  if (freshLanding === null && observedLanding !== null) setLanding({ scope, fresh: observedLanding });
  const awaitingGeneral = freshLanding === null && !rooms.roomsLoading;
  useEffect(() => {
    if (!awaitingGeneral) return;
    const timer = setTimeout(() => setLanding((current) => current?.scope === scope ? current : { scope, fresh: false }), FRESH_LANDING_WAIT_MS);
    return () => clearTimeout(timer);
  }, [awaitingGeneral, scope]);
  const setRoomTyping = rooms.setTyping;
  const sendRoomMessage = rooms.sendMessage;
  const teams = useTeamsApi();
  // The runtime is still being set up (a new workspace): a Router room's first message shows as queued.
  const { setupStarting } = useChatReady();
  const { agents } = useAgents();
  const roster = useRosterEntries(agents);
  const activityByAgentId = useMemo(() => new Map(roster.map((entry) => [entry.agent.id, entry.state])), [roster]);
  const { data: agentRoster } = teams.read<{ agents?: CompanyAgent[] }>("/agents");
  const { data: humanRoster } = teams.read<{ members?: CompanyMember[] }>("/members");
  const [draft, setDraft] = useState("");
  const [clientMessageId, setClientMessageId] = useState(() => crypto.randomUUID());
  const [threadId, setThreadId] = useState<string | null>(null);
  const [notificationTarget, setNotificationTarget] = useState<{ roomId: string; threadId: string | null } | null>(null);
  useEffect(() => {
    if (!notificationTarget || notificationTarget.roomId !== rooms.activeRoomId) return;
    setThreadId(notificationTarget.threadId);
    setNotificationTarget(null);
  }, [notificationTarget, rooms.activeRoomId]);
  const [threadDraft, setThreadDraft] = useState("");
  const [threadClientMessageId, setThreadClientMessageId] = useState(() => crypto.randomUUID());
  const clientMessageIdRef = useRef(clientMessageId);
  const threadClientMessageIdRef = useRef(threadClientMessageId);
  clientMessageIdRef.current = clientMessageId;
  threadClientMessageIdRef.current = threadClientMessageId;
  // Read in the send failure path, which resolves long after its closure was
  // built and must know where the owner actually is now.
  const activeRoomIdRef = useRef(rooms.activeRoomId);
  activeRoomIdRef.current = rooms.activeRoomId;
  const threadIdRef = useRef<string | null>(threadId);
  threadIdRef.current = threadId;
  const activeRoomKind = rooms.activeRoom?.kind;
  const activeRoomKindRef = useRef(activeRoomKind);
  activeRoomKindRef.current = activeRoomKind;
  // One room_opened per room the person lands in: explicit picks and the
  // fresh-workspace auto-open of #general alike. room_kind only — a room's
  // name can be customer content. Before this, the only trace that a new
  // owner had seen the welcome message was a read cursor in DynamoDB.
  useEffect(() => {
    if (!rooms.activeRoomId || !activeRoomKind) return;
    capture("room_opened", { room_kind: activeRoomKind, workspace_id: orgId ?? null });
  }, [rooms.activeRoomId, activeRoomKind, orgId]);
  // composer_typing_started, once per room per mount: separates "looked and
  // left" from "started to type and gave up" for a new owner who never sends.
  const typedRoomsRef = useRef<Set<string>>(new Set());
  // Mirror the live composer text. Read in the send failure path to decide
  // whether there is anything there worth protecting.
  const draftRef = useRef(draft);
  draftRef.current = draft;
  const threadDraftRef = useRef(threadDraft);
  threadDraftRef.current = threadDraft;
  const [creationBusy, setCreationBusy] = useState(false);
  const [openingParticipant, setOpeningParticipant] = useState<string | null>(null);
  // Bumped whenever a draft is written for a room OTHER than the active one —
  // which only the send-failure path does. Without it the sidebar's draft dot
  // and Drafts section would not show a recovered draft until some unrelated
  // render happened to recompute them.
  const [recoveredDrafts, setRecoveredDrafts] = useState(0);
  const [creationError, setCreationError] = useState<string>();
  // A failed DM resolve-or-create (rooms.selectRoomError) otherwise vanishes:
  // the caller rethrows but nobody renders it. Dismissal is local — the hook
  // itself clears the error on the next successful select or create.
  const [selectErrorDismissed, setSelectErrorDismissed] = useState<string | null>(null);
  const selectRoomError = rooms.selectRoomError && rooms.selectRoomError !== selectErrorDismissed
    ? rooms.selectRoomError
    : null;
  // A fresh workspace has no rooms until setup seeds #general, so the viewer
  // sits on an empty pane (or on the vendor shared room, which the shared
  // hook's rooms[0] pick can land on). Open #general once per workspace — but
  // only while the workspace is FRESH (nothing but #general and the shared
  // room exists) and the viewer has not chosen a room themselves. A returning
  // viewer has other rooms, so the shared hook's most-recent pick stands.
  // Both refs hold the scope they apply to, so switching workspaces re-arms.
  const explicitChoiceScope = useRef<string | null>(null);
  const autoOpenedScope = useRef<string | null>(null);
  // Counts every explicit choice. The desktop shell's history (ChatHistorySync)
  // records a view only when this moved, so automatic picks stay out of it. An
  // async pick bumps it when its room lands, not before: an early bump would be
  // recorded against the view being left.
  const [explicitPickCount, setExplicitPickCount] = useState(0);
  const bumpPick = useCallback(() => setExplicitPickCount((n) => n + 1), []);
  // An async pick that finishes after the person navigated elsewhere (another
  // panel) must not bump: ChatHistorySync would push the DM URL over theirs.
  // Read-only on the URL.
  const bumpPickIfCurrent = useCallback((start: PickToken) => {
    if (pickTokenUnchanged(start, pickToken())) bumpPick();
  }, [bumpPick]);
  const markExplicitScope = useCallback(() => { explicitChoiceScope.current = scope; }, [scope]);
  const markExplicitChoice = useCallback(() => {
    markExplicitScope();
    bumpPick();
  }, [markExplicitScope, bumpPick]);
  const { activeRoomId: autoOpenActive, rooms: autoOpenRooms, selectRoom: hookSelectRoom } = rooms;
  /** Every selection a person (or a link/notification they followed) makes. */
  const chooseRoom = useCallback((roomId: string) => {
    markExplicitChoice();
    hookSelectRoom(roomId);
  }, [markExplicitChoice, hookSelectRoom]);
  useEffect(() => {
    if (autoOpenedScope.current === scope || explicitChoiceScope.current === scope) return;
    const general = autoOpenRooms.find((room) => room.isDefault);
    if (!general || !isFreshWorkspace(autoOpenRooms)) return;
    autoOpenedScope.current = scope;
    if (autoOpenActive !== general.roomId) hookSelectRoom(general.roomId);
  }, [scope, autoOpenActive, autoOpenRooms, hookSelectRoom]);
  const [shortcutsOpen, setShortcutsOpen] = useState(false);
  const toggleShortcuts = useCallback(() => setShortcutsOpen((open) => !open), []);
  useMessagingShortcuts(rooms.rooms, rooms.activeRoomId, chooseRoom, toggleShortcuts, shortcutsOpen);
  const [draftRoomId, setDraftRoomId] = useState(rooms.activeRoomId);
  const [draftThreadId, setDraftThreadId] = useState<string | null>(null);
  const typingActive = useRef(false);
  const typingTimer = useRef<ReturnType<typeof setTimeout> | null>(null);

  // Reset before the new room's composer consumes a pending draft/focus
  // handoff — restoring, not clearing, so a half-typed message survives a
  // room switch (spec: per-room drafts). There is no separate `mentions`
  // state to restore: the composer derives mentions purely from the typed
  // text (resolveTypedMentions), so restoring `text` is enough — the
  // structured mentions come back on their own once the text re-renders.
  if (draftRoomId !== rooms.activeRoomId) {
    setDraftRoomId(rooms.activeRoomId);
    setDraft(rooms.activeRoomId ? readDraft(draftKey(scope, rooms.activeRoomId, null)).text : "");
    setClientMessageId(crypto.randomUUID());
    setThreadId(null);
    setDraftThreadId(null);
    setThreadDraft("");
    setThreadClientMessageId(crypto.randomUUID());
  } else if (draftThreadId !== threadId) {
    setDraftThreadId(threadId);
    setThreadDraft(rooms.activeRoomId && threadId ? readDraft(draftKey(scope, rooms.activeRoomId, threadId)).text : "");
    setThreadClientMessageId(crypto.randomUUID());
  }

  useEffect(() => () => {
    if (typingTimer.current) clearTimeout(typingTimer.current);
    if (typingActive.current) void setRoomTyping(false);
  }, [setRoomTyping]);

  const online = usePresence(rooms.httpReady, scope);
  // Ended sessions come too: All sessions lists the last week's.
  // The session sidebar stays visible when Tasks or another dock panel has focus.
  const workspaceSessions = useWorkspaceSessions(rooms.resourceSource, { enabled: rooms.httpReady, includeEnded: true });
  const sharing = useMemo(() => new Set(workspaceSessions.sessions.filter((session) => session.status !== "ended" && !sessionArchived(session))
    .map((session) => session.humanUserId)), [workspaceSessions.sessions]);
  // The unread dot: this viewer's last-seen seq per session, on this device.
  // ponytail: entries are never pruned (one number per session ever listed); prune if it ever matters.
  // Per person AND workspace (as the sidebar's pins and archives): two members on one browser profile
  // keep their own unread dots, and Mark unread/read moves only the signed-in person's.
  const seenScope = `${userId ?? ""}:${scope}`;
  const [sessionSeen, setSessionSeen] = useState(() => ({ scope: seenScope, seen: readSessionSeen(seenScope) }));
  const seen = sessionSeen.scope === seenScope ? sessionSeen.seen : readSessionSeen(seenScope);
  const openSessionId = sessionFocus?.sessionId ?? null;
  useEffect(() => {
    const next = markSessionsSeen(seen, workspaceSessions.sessions, openSessionId);
    if (next === seen && sessionSeen.scope === seenScope) return;
    setSessionSeen({ scope: seenScope, seen: next });
    writeSessionSeen(seenScope, next);
  }, [seen, workspaceSessions.sessions, openSessionId, seenScope, sessionSeen.scope]);
  // A room member's online/sharing (human) or agentActivity (agent) decoration --
  // the same ternary every member list below applies to its own room-member array.
  // Super-agent: the front agent is "Lightbulb" (@lightbulb) everywhere, whatever its roster name ("CEO").
  // Stored member rows and the Paperclip roster keep the old name, so the one rename happens here.
  const superAgent = useSuperAgent();
  // Until the roster answers, the front agent is the one this browser last saw (else, on a first load, an agent still
  // named for its seat): a hard load never flashes "CEO" before "Lightbulb" (dev walk R6).
  const rosterFrontId = superAgent ? resolveCeoAgentId(agentRoster?.agents ?? []) : undefined;
  useEffect(() => { if (rosterFrontId) writeFrontAgentId(scope, rosterFrontId); }, [scope, rosterFrontId]);
  const rosterLoading = superAgent && !agentRoster;
  const frontId = rosterFrontId ?? (rosterLoading ? readFrontAgentId(scope) : undefined);
  const isFront = useCallback((member: Pick<RoomMember, "id" | "displayName">) => member.id === frontId
    || (rosterLoading && !frontId && member.displayName === SEAT_NAME), [frontId, rosterLoading]);
  const decorate = useCallback((member: RoomMember): RoomParticipant => member.kind === "agent"
    ? { ...member, ...(isFront(member) ? { displayName: FRONT_AGENT_NAME, mentionHandle: "lightbulb" } : {}), agentActivity: activityByAgentId.get(member.id) }
    : { ...member, online: online.has(member.id), sharing: sharing.has(member.id) },
  [activityByAgentId, online, sharing, isFront]);
  // A roster row's display name, with the same "Workspace member" fallback everywhere one is missing.
  const rosterName = (member: CompanyMember | undefined) => member?.name || member?.email || "Workspace member";
  const { user: clerkUser } = useUser();
  const myName = clerkUser?.fullName || clerkUser?.primaryEmailAddress?.emailAddress || "Workspace member";
  const isOnRoster = useCallback((id: string) => Boolean(humanRoster?.members?.some((candidate) => candidate.userId === id)), [humanRoster]);
  const humanName = useCallback((id: string) => rosterName(humanRoster?.members?.find((candidate) => candidate.userId === id)), [humanRoster]);
  // The same owner-name/own resolution sidebarSessions applies per section, for a
  // session fetched directly rather than found in the workspace list (SessionFocus's
  // single-session fallback).
  const resolveSessionOwner = useCallback((humanUserId: string) =>
    ({ ownerName: humanUserId === userId ? "You" : humanName(humanUserId), own: humanUserId === userId }),
    [userId, humanName]);
  const sessionRows = useSessionRows(workspaceSessions.sessions, userId, humanName, isOnRoster);
  const sidebarSessions: SidebarSession[] = useMemo(() =>
    workspaceSections(sessionRows.flatMap((row) => row.backend ?? []), userId, humanName).flatMap((section) =>
      section.data.map((session) => ({ session, ownerName: section.title, own: section.key === userId,
        owner: ownerFace(session.humanUserId, session.humanUserId === userId && !isOnRoster(session.humanUserId) ? myName : humanName(session.humanUserId)), unread: sessionUnread(session, seen) }))),
    [sessionRows, userId, humanName, isOnRoster, myName, seen]);
  const sidebarRows: SidebarRow[] = useMemo(() => sessionRows.map((row) => ({
    row,
    owner: ownerFace(row.person.userId ?? row.id, row.person.isViewer && !(row.person.userId && isOnRoster(row.person.userId)) ? myName : row.person.name),
    unread: row.backend ? sessionUnread(row.backend, seen) : null,
  })), [sessionRows, isOnRoster, myName, seen]);
  const members: RoomParticipant[] = useMemo(() => rooms.activeRoom?.members.map(decorate) ?? [], [rooms.activeRoom, decorate]);
  // The feed seats only people; its agents are authors on the roster. They are added here so a
  // Discuss reply can @mention one (by name: an unseated agent has no handle in the room).
  const activeFeed = Boolean(rooms.activeRoom?.feed);
  const feedRoster = activeFeed ? agentRoster?.agents : undefined;
  const participants: RoomParticipant[] = useMemo(() => feedRoster ? [...members, ...feedRoster
    .filter((agent) => !members.some((member) => member.kind === "agent" && member.id === agent.id))
    .map((agent) => decorate({ kind: "agent", id: agent.id, displayName: agent.name, mentionHandle: null,
      role: "member", listenMode: null, joinedAt: "", joinedSeq: 0 }))] : members, [members, feedRoster, decorate]);
  const sessionCandidates = useMemo(() => sessionMentionCandidates(
    workspaceSessions.sessions.filter((session) => session.kind !== "background"),
  ), [workspaceSessions.sessions]);
  // What a reference card resolves against: every interactive session the viewer can open,
  // not the sendable subset below (an offline, ended or read-only teammate session is still a reference).
  const visibleSessions = useMemo(() => sessionCandidates.map(({ session }) => session), [sessionCandidates]);
  // Reference rule (#3623): shared session whose owner is in the room; no send/control grant needed.
  const sessionTargets = useMemo(() => rooms.activeRoom?.shared ? [] : sessionCandidates
    .filter(({ session }) => session.shared !== false
      && rooms.activeRoom?.members.some((member) => member.kind === "human" && member.id === session.humanUserId))
    .map((candidate) => ({ candidate, ...resolveSessionOwner(candidate.session.humanUserId) })),
  [rooms.activeRoom?.shared, rooms.activeRoom?.members, sessionCandidates, resolveSessionOwner]);
  const directoryRooms: DirectoryRoom[] = useMemo(() => rooms.rooms.map((room) => ({
    ...room,
    // Straight off the room summary — this used to come from a per-DM
    // `getRoom` fan-out, so until it settled every DM row read "Direct message".
    participants: (room.members ?? [])
      .filter((member) => member.kind !== "human" || member.id !== userId)
      .map(decorate),
  })), [rooms.rooms, userId, decorate]);
  const directoryParticipants: DirectoryParticipant[] = useMemo(() => {
    const people = (humanRoster?.members ?? [])
      .filter((member) => member.userId !== userId)
      .map((member) => ({
      kind: "human" as const,
      id: member.userId,
      displayName: rosterName(member),
      mentionHandle: null,
      online: online.has(member.userId),
      sharing: sharing.has(member.userId),
    }));
    const agents = (agentRoster?.agents ?? []).map((agent) => ({
      kind: "agent" as const,
      id: agent.id,
      displayName: agent.id === frontId ? FRONT_AGENT_NAME : agent.name,
      agentActivity: activityByAgentId.get(agent.id),
      mentionHandle: rooms.activeRoom?.members.find((member) => member.kind === "agent" && member.id === agent.id)?.mentionHandle ?? null,
    }));
    return [...people, ...agents];
  }, [humanRoster, agentRoster, rooms.activeRoom, userId, online, sharing, activityByAgentId, frontId]);

  const handleTyping = useCallback((isTyping: boolean) => {
    if (typingTimer.current) clearTimeout(typingTimer.current);
    if (!isTyping) {
      if (typingActive.current) void setRoomTyping(false);
      typingActive.current = false;
      return;
    }
    if (!typingActive.current) {
      void setRoomTyping(true);
      const roomId = activeRoomIdRef.current;
      if (roomId && !typedRoomsRef.current.has(roomId)) {
        typedRoomsRef.current.add(roomId);
        capture("composer_typing_started", {
          room_kind: activeRoomKindRef.current ?? null,
          workspace_id: orgId ?? null,
        });
      }
    }
    typingActive.current = true;
    typingTimer.current = setTimeout(() => {
      typingActive.current = false;
      void setRoomTyping(false);
    }, 3_000);
  }, [setRoomTyping, orgId]);

  // Persist as the owner types — recomputed structured mentions come from
  // the same resolveTypedMentions the send path uses, so a restored draft's
  // mentions are exactly what would be dispatched if sent as-is.
  useEffect(() => {
    if (!rooms.activeRoomId) return;
    writeDraft(draftKey(scope, rooms.activeRoomId, null), { text: draft, mentions: resolveTypedMentions(draft, participants) });
  }, [scope, rooms.activeRoomId, draft, participants]);

  useEffect(() => {
    if (!rooms.activeRoomId || !threadId) return;
    writeDraft(draftKey(scope, rooms.activeRoomId, threadId), { text: threadDraft, mentions: resolveTypedMentions(threadDraft, participants) });
  }, [scope, rooms.activeRoomId, threadId, threadDraft, participants]);

  /**
   * Park a failed message's text in a room's stored draft — but never on top of
   * something newer.
   *
   * There is one draft slot per room and, in this path, two possible claimants:
   * the message that just failed and whatever the owner has typed since. The
   * live text is the owner's current intent and exists nowhere else, so it
   * wins; the failed text is dropped rather than allowed to overwrite it.
   *
   * ponytail: a failed message should live in the timeline as a failed row with
   * its own retry, the way Slack does it, which removes the contention
   * entirely. That needs a rejected state on the pending row in RoomPanel —
   * `useRooms` currently drops the optimistic entry on failure — and is not
   * this change.
   */
  const recoverDraft = useCallback((key: string, request: CreateMessageRequest) => {
    if (readDraft(key).text.trim()) return;
    writeDraft(key, { text: request.text, mentions: request.mentions });
    setRecoveredDrafts((count) => count + 1);
  }, []);

  const send = useCallback(async (request: CreateMessageRequest, thread = false) => {
    // Read before the await: this is the room the message was sent FROM, even
    // if the owner switches rooms while it is in flight.
    const roomId = rooms.activeRoomId;
    // Empty the box NOW, before the round trip — not after it.
    //
    // This used to await the POST first, and because `sendState` is "sending"
    // for that whole flight the textarea was also disabled, so a sender watched
    // their own text sit frozen in the composer while an optimistic copy of it
    // rendered in the timeline below. Clearing on the keystroke is what makes
    // sending feel instant; the optimistic row already carries the pending,
    // failed and uncertain states, and `reconcileSend` still holds the request
    // by its client id, so nothing is lost if the send does not land.
    let rotatedTo: string | null = null;
    if (thread) {
      if (roomId && request.threadRootMessageId) clearDraft(draftKey(scope, roomId, request.threadRootMessageId));
      if (threadClientMessageIdRef.current === request.clientMessageId) {
        rotatedTo = crypto.randomUUID();
        // The ref mirrors the composer's CURRENT id, so it has to move now
        // rather than at the next render — a send that rejects in the same
        // microtask would otherwise read a stale ref and conclude the composer
        // had moved on when it had not.
        threadClientMessageIdRef.current = rotatedTo;
        threadDraftRef.current = "";
        setThreadDraft("");
        setThreadClientMessageId(rotatedTo);
      }
    } else {
      if (roomId) clearDraft(draftKey(scope, roomId, null));
      if (clientMessageIdRef.current === request.clientMessageId) {
        rotatedTo = crypto.randomUUID();
        clientMessageIdRef.current = rotatedTo;   // see the thread branch above
        draftRef.current = "";
        setDraft("");
        setClientMessageId(rotatedTo);
      }
    }
    try {
      await sendRoomMessage(request);
    } catch (error) {
      // Give it back. The composer has already moved on to a fresh id, so
      // without this the failed message has nowhere to live: its optimistic row
      // is dropped by `useRooms` on failure, its text was cleared, and both
      // "Retry message" and "Check message status" would act on an id that
      // tracks nothing. Re-adopting the failed id is what makes `sendState`
      // report rejected/uncertain again and points retry at the real request.
      const sameRoom = activeRoomIdRef.current === roomId;
      // ...but only into the composer this send actually left behind. Sending a
      // second message, or typing into the fresh box, rotates the id again —
      // and re-adopting the failed one then remounts the composer and destroys
      // that newer, unsent work (its text and any attached-but-unsent images).
      // Newer work wins: it is the owner's live intent, and unlike the failed
      // message it exists nowhere else.
      // Two ways the composer stops being the empty box this send left behind,
      // and BOTH have to be checked: sending again rotates the id, but simply
      // TYPING does not — it only fills the box. Restoring over either is the
      // same data loss, so the box must still be the one we rotated to AND
      // still be empty.
      const composerHeld = !thread && rotatedTo !== null
        && clientMessageIdRef.current === rotatedTo && draftRef.current.trim() === "";
      const threadComposerHeld = thread && rotatedTo !== null
        && threadClientMessageIdRef.current === rotatedTo && threadDraftRef.current.trim() === "";
      if (thread) {
        if (sameRoom && threadComposerHeld && threadIdRef.current === request.threadRootMessageId) {
          setThreadDraft(request.text);
          setThreadClientMessageId(request.clientMessageId);
        } else if (roomId && request.threadRootMessageId) {
          recoverDraft(draftKey(scope, roomId, request.threadRootMessageId), request);
        }
      } else if (sameRoom && composerHeld) {
        setDraft(request.text);
        setClientMessageId(request.clientMessageId);
      } else if (roomId) {
        // Nowhere live to put it — the owner is in another room, or has moved
        // the composer on. It goes to that room's stored draft instead.
        recoverDraft(draftKey(scope, roomId, null), request);
      }
      // Rethrown: the composer's own submit() turns a rejection into its
      // uncertain state, and that path must keep working.
      throw error;
    }
  }, [sendRoomMessage, rooms.activeRoomId, scope, recoverDraft]);

  // "Check message status" replays the send by its client id (the server answers a
  // replay with the stored message, never a second copy). Once that confirms it, the
  // box must move on to a fresh id like a send does -- a composer still holding a
  // persisted id stays locked with the sent text in it.
  const reconcileRoomSend = rooms.reconcileSend;
  const reconcileSend = useCallback(async (clientMessageId: string, thread = false) => {
    if (!await reconcileRoomSend(clientMessageId)) return;
    if (thread && threadClientMessageIdRef.current === clientMessageId) {
      const next = crypto.randomUUID();
      threadClientMessageIdRef.current = next;
      threadDraftRef.current = "";
      setThreadDraft("");
      setThreadClientMessageId(next);
    } else if (!thread && clientMessageIdRef.current === clientMessageId) {
      const next = crypto.randomUUID();
      clientMessageIdRef.current = next;
      draftRef.current = "";
      setDraft("");
      setClientMessageId(next);
    }
  }, [reconcileRoomSend]);

  // localStorage is the source, so no prop dependency truly describes when this
  // changes — but it walks every key in the store and JSON.parses each hit, and
  // every writer is in this component: the draft effects below, and the
  // send-failure path, which is the only one that can write for a room the
  // owner has already left (hence `recoveredDrafts`). Recomputing when one of
  // those moves is enough, and keeps the walk off the keystroke path for every
  // other room's row.
  // Emptying the box (a send, a select-all delete) drops the stored draft now, before the render that
  // lists draft rooms: the persist effect runs after it, so the sidebar kept "Drafts › room" until something else
  // re-rendered (dev walk R10).
  const activeRoomId = rooms.activeRoomId;
  const changeDraft = useCallback((text: string) => {
    if (!text.trim() && activeRoomId) clearDraft(draftKey(scope, activeRoomId, null));
    setDraft(text);
  }, [scope, activeRoomId]);
  // Read back from storage AFTER the persist effects above have written this render's text (an effect declared later
  // runs later). Computed during render it saw the previous write, so a drafted "@growth " that was then cleared, or a
  // first keystroke, left the sidebar one change behind (dev walk W8-11).
  const [draftRoomIds, setDraftRoomIds] = useState<string[]>(() => listDraftRoomIds(scope));
  // Compared HERE, not in a setState updater: an updater that returns the same list still schedules a render (the
  // keystroke's fiber is not idle yet), so every keystroke left one default-lane update behind. Chrome runs queued key
  // events before that work, each one counted as a nested update, and past 50 the next keystroke's setDraft threw
  // React #185 and its character was lost (dev walk W9-2).
  useEffect(() => {
    const next = listDraftRoomIds(scope);
    if (next.length !== draftRoomIds.length || next.some((id, i) => id !== draftRoomIds[i])) setDraftRoomIds(next);
  }, [scope, draft, threadDraft, rooms.activeRoomId, recoveredDrafts, draftRoomIds]);

  const agentCounterparts = participants.filter((member) => member.kind === "agent");
  const runningAgentIds = rooms.runs.filter((run) => run.state === "running").map((run) => run.agentId);
  const { account: billingAccount, refresh: refreshBilling } = useBilling();
  // A system notice is the platform reporting a budget block on a run; re-read
  // the account so the limit bar appears with it (the read never polls).
  const lastSystemNoticeId = rooms.messages.findLast((message) => message.author.kind === "system")?.messageId;
  useEffect(() => {
    if (lastSystemNoticeId) void refreshBilling();
  }, [lastSystemNoticeId, refreshBilling]);
  const composer: RoomComposerProps = {
    focusAgentId: rooms.activeRoom?.kind === "dm" && agentCounterparts.length === 1 ? agentCounterparts[0].id : undefined,
    text: draft,
    onTextChange: changeDraft,
    members: participants,
    sessionTargets,
    clientMessageId,
    sendState: rooms.sendState(clientMessageId),
    onSubmit: (request) => send(request),
    onRetry: (request) => send(request),
    onReconcile: (id) => void reconcileSend(id).catch(() => {}),
    disabled: !rooms.httpReady,
    onTypingChange: handleTyping,
    onUploadAttachment: rooms.uploadAttachment,
    roomKind: rooms.activeRoom?.kind,
    runningAgentIds,
  };

  const threadComposer: { parentMessageId: string; composer: RoomComposerProps } | undefined = threadId ? {
    parentMessageId: threadId,
    composer: {
      text: threadDraft,
      onTextChange: setThreadDraft,
      members: participants,
      sessionTargets,
      clientMessageId: threadClientMessageId,
      sendState: rooms.sendState(threadClientMessageId),
      onSubmit: (request: CreateMessageRequest) => send(request, true),
      onRetry: (request: CreateMessageRequest) => send(request, true),
      onReconcile: (id: string) => void reconcileSend(id, true).catch(() => {}),
      replyToMessageId: threadId,
      threadRootMessageId: threadId,
      disabled: !rooms.httpReady,
      onTypingChange: handleTyping,
      onUploadAttachment: rooms.uploadAttachment,
      roomKind: rooms.activeRoom?.kind,
      runningAgentIds,
    },
  } : undefined;

  async function createRoom(creation: ConversationCreation) {
    setCreationBusy(true);
    setCreationError(undefined);
    try {
      markExplicitScope();
      const startToken = pickToken();
      await rooms.createRoom({ clientRoomId: crypto.randomUUID(), ...creation });
      bumpPickIfCurrent(startToken);
      return true;
    } catch (error) {
      setCreationError(error instanceof Error ? error.message : "The chat could not be created.");
      return false;
    } finally {
      setCreationBusy(false);
    }
  }

  async function selectParticipant(participant: DirectoryParticipant) {
    // Only the row being opened goes busy. This used to set `creationBusy`,
    // which disables EVERY roster row, so opening one conversation froze the
    // whole list for the duration of the round trip.
    setOpeningParticipant(`${participant.kind}:${participant.id}`);
    setCreationError(undefined);
    try {
      markExplicitScope();
      const startToken = pickToken();
      if (await rooms.selectDm(participant)) bumpPickIfCurrent(startToken);
    } catch (error) {
      setCreationError(error instanceof Error ? error.message : "The chat could not be opened.");
    } finally {
      setOpeningParticipant(null);
    }
  }

  async function changeListenMode(agentId: string, listenMode: ListenMode) {
    const member = participants.find((candidate) => candidate.kind === "agent" && candidate.id === agentId);
    if (!member) return;
    await rooms.upsertMember({ kind: "agent", id: agentId, role: "member", listenMode });
  }

  async function addMember(member: MemberRef) {
    await rooms.upsertMember({
      kind: member.kind,
      id: member.id,
      role: "member",
      listenMode: member.kind === "agent" ? "active" : null,
    });
  }

  // Permalink target: opened via openMessage (a room permalink), cleared 3s
  // after opening rather than persisting indefinitely once the highlight has
  // had time to land.
  const [target, setTarget] = useState<{ roomId: string; messageId: string | null } | null>(null);
  const selectRoom = chooseRoom;
  const selectDm = rooms.selectDm;
  const closeSession = useCallback(() => setSessionFocus(null), []);
  /**
   * Focus the session NOW, whatever the session list knows. A link applied before the list lands
   * (a reload, a new tab, a deep link) used to wait for it, and the restored room stood in with a
   * live composer: a person typing right after a reload posted into that room (prod 2026-10-07).
   * SessionFocus owns the wait ("Opening session…") and the unknown id ("no longer shared").
   * Returns false for such an early link: it is the URL's replay, not a pick.
   */
  const applySession = useCallback((sessionId: string, chat = false, seq?: number) => {
    setThreadId(null);
    setTarget(null);
    // Opening the URL this open just wrote (?session=, no seq) replays it: keep the turn it opened at.
    setSessionFocus((current) => {
      const at = seq ?? (current?.sessionId === sessionId ? current.seq : undefined);
      return { sessionId, ...(chat ? { chat } : {}), ...(at ? { seq: at } : {}) };
    });
    return !(workspaceSessions.loading && !workspaceSessions.sessions.some((candidate) => candidate.sessionId === sessionId));
  }, [workspaceSessions.sessions, workspaceSessions.loading]);
  // A person's open is a pick; a link applied before the list lands is not.
  const openSession = useCallback((sessionId: string, chat = false, seq?: number) => {
    if (applySession(sessionId, chat, seq)) bumpPick();
  }, [applySession, bumpPick]);
  const setSessionChat = useCallback((open: boolean) => {
    setSessionFocus((current) => current?.sessionId ? { ...current, chat: open } : current);
  }, []);
  const openNewSession = useCallback(() => {
    setThreadId(null);
    setTarget(null);
    setSessionFocus({ sessionId: null });
  }, []);
  const openAllSessions = useCallback(() => {
    setThreadId(null);
    setTarget(null);
    setSessionFocus({ sessionId: null, all: true });
  }, []);
  const openMessage = useCallback((roomId: string, messageId: string | null) => {
    closeSession();
    selectRoom(roomId);
    setTarget({ roomId, messageId });
  }, [selectRoom, closeSession]);
  const previousSelectedRoom = useRef(rooms.activeRoomId);
  useEffect(() => {
    const moved = previousSelectedRoom.current !== rooms.activeRoomId;
    // Room changes that bypass the directory (a notification, ⌥↑/⌥↓) leave focus
    // too. Automatic picks (the cold load's first room, the default agent's DM,
    // #general's auto-open) are not a move away from anything: a session deep link
    // that landed first stays open. Only a person's choice marks the scope explicit.
    if (moved && previousSelectedRoom.current !== null && explicitChoiceScope.current === scope) setSessionFocus(null);
    previousSelectedRoom.current = rooms.activeRoomId;
  }, [rooms.activeRoomId, scope]);
  const targetRoomId = rooms.activeRoomId;
  const targetMessages = rooms.messages;
  const targetHistoryComplete = rooms.historyComplete;
  const targetHistoryLoading = rooms.historyLoading;
  const targetHistoryError = rooms.historyError;
  const loadTargetHistory = rooms.loadHistory;
  useEffect(() => {
    if (!target?.messageId) return;
    if (target.roomId !== targetRoomId) return;
    if (!targetMessages.some((message) => message.messageId === target.messageId) && !targetHistoryComplete && !targetHistoryLoading && !targetHistoryError) {
      loadTargetHistory();
      return;
    }
    if (!targetHistoryComplete && !targetMessages.some((message) => message.messageId === target.messageId) && !targetHistoryError) return;
    const timer = setTimeout(() => setTarget(null), 3_000);
    return () => clearTimeout(timer);
  }, [target, targetRoomId, targetMessages, targetHistoryComplete, targetHistoryLoading, targetHistoryError, loadTargetHistory]);

  const readSeqAtOpen = useRef<{ roomId: string; seq: number | null } | null>(null);
  if (rooms.activeRoom && readSeqAtOpen.current?.roomId !== rooms.activeRoom.roomId) readSeqAtOpen.current = { roomId: rooms.activeRoom.roomId, seq: rooms.activeRoom.lastReadSeq ?? null };

  // `directoryLoading` is gone with the fan-out that produced it: the room list
  // is now the whole sidebar, so there is nothing else to wait for.
  const directoryState = rooms.roomsLoading ? "loading" : rooms.roomsError ? "error" : "ready";
  // Messages in hand beat a pending room-detail read. The detail GET only adds
  // the delivery key and pinned ids; blocking the whole panel on it meant a
  // room you opened a moment ago still showed "Loading conversation…".
  const roomState = rooms.roomLoading && rooms.messages.length === 0 ? "loading"
    : rooms.accessRemoved ? "access-removed"
    : rooms.roomError ? "unavailable"
    : rooms.liveConnected ? "ready" : "reconnecting";
  // Every message still in flight for this room, NOT just the one the composer
  // happens to be pointing at. The composer rotates to a fresh id the moment a
  // send leaves, so filtering by it here meant the "Sending…" bubble matched
  // nothing and the message vanished until the server answered — the opposite
  // of the point. `useRooms` already scopes these to the active room and clears
  // them on a room change, and RoomPanel splits main-timeline from thread.
  const pending = rooms.optimisticMessages;
  const viewerRole = rooms.activeRoom?.members.find((member) => member.kind === "human" && member.id === userId)?.role ?? "";
  // Any member may change an agent's response, on every plan (owner ruling 2026-09-24).
  const canManageResponses = canChangeListenMode(viewerRole);

  const directoryProps: RoomDirectoryProps = {
    ownerScopeId: scope,
    rooms: directoryRooms,
    sections: rooms.sections,
    participants: directoryParticipants,
    ceoAgentId: resolveCeoAgentId(agentRoster?.agents ?? []) ?? (rosterLoading ? frontId : undefined),
    state: directoryState,
    currentRoomId: rooms.activeRoomId ?? undefined,
    // Picking any conversation leaves focus placement — the session goes back
    // to being something you open, not the thing you are looking at.
    onSelectRoom: (roomId: string) => { closeSession(); chooseRoom(roomId); },
    currentUserId: userId ?? undefined,
    onAddPeople: (roomId: string) => { closeSession(); chooseRoom(roomId); setAddPeopleRoomId(roomId); },
    onSelectParticipant: async (participant: DirectoryParticipant) => { closeSession(); await selectParticipant(participant); },
    openingParticipant,
    onMarkUnread: rooms.markUnread,
    onSetPrefs: rooms.setRoomPrefs,
    onSaveSections: rooms.saveSections,
    onLeaveRoom: rooms.leaveRoom,
    onCreate: createRoom,
    creationBusy,
    creationError,
    onRetry: () => void rooms.refreshRooms(),
    draftRoomIds,
    sessions: sidebarSessions,
    sessionRows: sidebarRows,
    onShareSession: shareEngineSession,
    onSelectSession: (sessionId: string) => void openSession(sessionId),
    onRenameSession: rooms.resourceSource.client.setLocalSessionDisplayTitle ? async (sessionId, title) => {
      await rooms.resourceSource.client.setLocalSessionDisplayTitle!(sessionId, title);
      await workspaceSessions.refresh();
    } : undefined,
    // Nothing leaves the list until the server has removed it: a refusal (it resumed meanwhile, the network)
    // rejects here, and the button that asked says so. The panel showing the session closes itself (SessionFocus).
    // A running session is ended first and removed when its Mac has stopped it: its row stays, as running,
    // until the list hears of that end.
    onRemoveSession: async (sessionId) => {
      await rooms.resourceSource.client.removeLocalSession(sessionId);
      workspaceSessions.forget((session) => session.sessionId === sessionId && session.status === "ended");
    },
    onRemoveEndedSessions: async () => {
      try { await rooms.resourceSource.client.removeEndedLocalSessions(); }
      finally { await workspaceSessions.refresh(); }
    },
    onMarkSessionUnread: (sessionId, unread) => {
      const session = workspaceSessions.sessions.find((candidate) => candidate.sessionId === sessionId);
      if (!session) return;
      const next = (unread ? markSessionUnread : markSessionRead)(seen, session);
      setSessionSeen({ scope: seenScope, seen: next });
      writeSessionSeen(seenScope, next);
    },
    currentSessionId: sessionFocus?.sessionId ?? null,
    onOpenAllSessions: openAllSessions,
    allSessionsOpen: !!sessionFocus?.all,
  };
  const selectMessageParticipant = useCallback(async (participant: RoomParticipant) => {
    markExplicitScope();
    const startToken = pickToken();
    if (await selectDm(participant)) bumpPickIfCurrent(startToken);
  }, [markExplicitScope, bumpPickIfCurrent, selectDm]);
  const { selectAgentDm: hookSelectAgentDm } = rooms;
  const selectAgentDm = useCallback((agentId: string, explicit = true) => {
    if (!explicit) return hookSelectAgentDm(agentId);
    markExplicitScope();
    const startToken = pickToken();
    return hookSelectAgentDm(agentId).then((roomId) => {
      if (roomId) bumpPickIfCurrent(startToken);
      return roomId;
    });
  }, [markExplicitScope, bumpPickIfCurrent, hookSelectAgentDm]);
  const { activeRoomId: unreadRoomId, markUnread: markRoomUnread } = rooms;
  const markUnreadFrom = useCallback(async (fromSeq: number) => {
    if (unreadRoomId) await markRoomUnread(unreadRoomId, fromSeq);
  }, [unreadRoomId, markRoomUnread]);
  // Stable identity: a fresh function per render would re-render every message on each keystroke.
  const onCardReply = useCallback(async (reply: CardReply) => { await sendRoomMessage(cardReplyRequest(reply)); }, [sendRoomMessage]);
  const roomProps: RoomPanelProps | null = rooms.activeRoomId ? {
    onCardReply,
    ownerScopeId: scope,
    currentUserId: userId ?? "",
    roomId: rooms.activeRoomId,
    room: rooms.activeRoom,
    readSeqAtOpen: readSeqAtOpen.current?.roomId === rooms.activeRoomId ? readSeqAtOpen.current.seq : null,
    targetMessageId: target?.roomId === rooms.activeRoomId ? target.messageId : null,
    state: roomState,
    httpReady: rooms.httpReady,
    liveConnected: rooms.liveConnected,
    participants,
    messages: rooms.messages,
    historyLoading: rooms.historyLoading,
    historyComplete: rooms.historyComplete,
    historyError: rooms.historyError,
    historyErrorMessage: rooms.historyErrorMessage,
    onLoadHistory: rooms.loadHistory,
    onLoadOlder: rooms.loadOlder,
    onJumpToMessage: (roomId: string, messageId: string) => setTarget({ roomId, messageId }),
    pendingMessages: pending,
    typingMembers: rooms.typingMembers.filter((member) => member.userId !== userId),
    runs: rooms.runs.map(runView),
    composer,
    threadComposer,
    sessions: visibleSessions,
    onSelectThread: setThreadId,
    selectedThreadId: threadId,
    onSelectParticipant: selectMessageParticipant,
    onRetry: () => void rooms.refreshRoom(),
    // Feed authors are not seated, so the feed has no listen modes to change (the service refuses membership edits).
    ...(canManageResponses && !activeFeed ? { onListenModeChange: (agentId: string, mode: ListenMode) => void changeListenMode(agentId, mode) } : {}),
    billing: billingAccount,
    onSetReaction: rooms.setReaction,
    onUpdateMessage: rooms.updateMessage,
    pinnedMessageIds: rooms.activeRoom?.pinnedMessageIds,
    onSetPin: rooms.setPin,
    onMarkUnreadFrom: markUnreadFrom,
    onSetPrefs: async (prefs) => { if (rooms.activeRoomId) await rooms.setRoomPrefs(rooms.activeRoomId, prefs); },
    // A group chat's creator holds no powers there, so anyone may leave or archive one.
    // The workspace feed is Lightbulb-managed: its people follow the workspace and its name is
    // fixed, so it offers no leave, archive, delete, rename, member or join-link controls; any
    // member may still edit its topic (the team's brief).
    onLeaveRoom: !activeFeed && (rooms.activeRoom?.kind === "group-dm" || rooms.activeRoom?.kind === "channel")
      ? async () => { if (rooms.activeRoomId) await rooms.leaveRoom(rooms.activeRoomId); } : undefined,
    ...(!activeFeed && (rooms.activeRoom?.kind === "group-dm" || (rooms.activeRoom?.kind === "channel" && ["owner", "admin"].includes(viewerRole)))
      ? { onSetArchived: async (archived: boolean) => { await rooms.updateRoom({ archived }); } } : {}),
    // Only the channel's owner -- not an admin -- may delete it (the backend enforces the same).
    ...(!activeFeed && rooms.activeRoom?.kind === "channel" && viewerRole === "owner"
      ? { onDeleteRoom: async () => { if (rooms.activeRoomId) await rooms.deleteRoom(rooms.activeRoomId); } } : {}),
    ...(activeFeed ? { onSetTopic: async (topic: string | null) => { await rooms.updateRoom({ topic }); } }
      : rooms.activeRoom && rooms.activeRoom.kind !== "dm" && ["owner", "admin"].includes(viewerRole) ? {
        onRenameRoom: async (name: string) => { await rooms.updateRoom({ name }); },
        onSetTopic: async (topic: string | null) => { await rooms.updateRoom({ topic }); },
      } : {}),
    ...(!activeFeed && rooms.activeRoom && !rooms.activeRoom.shared
      && (rooms.activeRoom.kind !== "channel" || ["owner", "admin"].includes(viewerRole)) ? {
      // Super-agent: people only; a specialist is reached through Lightbulb (Team › Ask), and Members can't remove one.
      addMemberCandidates: directoryParticipants.filter((candidate) => !(superAgent && candidate.kind === "agent")
        && !rooms.activeRoom!.members.some((member) => member.kind === candidate.kind && member.id === candidate.id)),
      onAddMember: addMember,
    } : {}),
    ...(!activeFeed && rooms.activeRoom?.kind === "channel" && ["owner", "admin"].includes(viewerRole)
      ? { onRemoveMember: rooms.removeMember } : {}),
    ...(!activeFeed && rooms.activeRoom && rooms.activeRoom.kind !== "dm" ? { onCopyJoinLink: copyJoinLink } : {}),
    openAddPeople: addPeopleRoomId !== null && addPeopleRoomId === rooms.activeRoomId,
    onAddPeopleOpened: () => setAddPeopleRoomId(null),
    onAbortRun: (runId) => rooms.abortRun(runId),
    subagents: rooms.subagents,
    delegationsByMessage: rooms.delegationsByMessage,
    messagingClient: rooms.resourceSource.client,
    onSteerSubagent: rooms.steerSubagent,
    onStopSubagent: rooms.stopSubagent,
    onSubagentsVisible: rooms.subagentsPollVisible,
    onLinkToWork: () => openPanel("teams-issues", {}, { pushUrl: true }),
    frontAgentId: resolveCeoAgentId(agentRoster?.agents ?? []) ?? (rosterLoading ? frontId : undefined),
    runtimeStarting: setupStarting,
    onDownloadAttachment: rooms.downloadAttachment,
    onAttachmentUrl: rooms.attachmentUrl,
  } : null;

  // Decided once per mount, when fresh-or-returning is known (see landsOnHome).
  const landingDecided = useRef(false);
  useEffect(() => {
    if (landingDecided.current || !superAgent || freshLanding === null) return;
    landingDecided.current = true;
    if (landsOnHome({ superAgent, fresh: freshLanding, search: window.location.search, snapshot: getWorkspaceSnapshot(), picked: explicitPickCount > 0 })) {
      openPanel("home");
    }
  }, [superAgent, freshLanding, explicitPickCount]);

  // Muted rooms never badge anywhere: not the sidebar, not the browser-tab title.
  const openRoomTarget = useCallback((roomId: string, targetThreadId: string | null, messageId?: string) => {
    // The room may be the one focus covers; opening it has to uncover it.
    closeSession();
    selectRoom(roomId);
    setNotificationTarget({ roomId, threadId: targetThreadId });
    // A room-level message is scrolled to, like a search hit; a thread reply is in the opened thread.
    if (messageId && !targetThreadId) setTarget({ roomId, messageId });
    activateMessagesPanel();
  }, [closeSession, selectRoom]);
  const askFrontId = directoryProps.ceoAgentId;
  const { selectDm: resolveDmRoom, resourceSource: { client: messagingClient } } = rooms;
  const askLightbulb = useCallback(async (text: string) => {
    if (!askFrontId) throw new Error("Lightbulb isn't ready yet.");
    // One resolve (it creates the DM when there is none), one send, then open that same room: nothing after the
    // send can fail, so a sent message is never reported as unsent. Sent first, so the DM's history load holds it.
    const roomId = await resolveDmRoom({ kind: "agent", id: askFrontId }, { activate: false });
    if (!roomId) throw new Error("Couldn't open your chat with Lightbulb.");
    await messagingClient.sendMessage(roomId, { clientMessageId: crypto.randomUUID(), text, mentions: [], replyToMessageId: null, threadRootMessageId: null });
    openRoomTarget(roomId, null);
  }, [askFrontId, resolveDmRoom, messagingClient, openRoomTarget]);
  const listedRoomIds = useMemo(() => new Set(rooms.rooms.map((room) => room.roomId)), [rooms.rooms]);
  const lookupRoom = rooms.resourceSource.client.getRoom;
  const openNotificationTarget = useCallback((roomId: string, targetThreadId: string | null, messageId?: string) => {
    if (listedRoomIds.has(roomId)) { openRoomTarget(roomId, targetThreadId, messageId); return; }
    // A session's chat is hidden from the room list: ask the room what it belongs to, and open the
    // session with its chat beside it rather than a bare room.
    void lookupRoom(roomId).then((room) => {
      if (room.session) { openSession(room.session.sessionId, true); activateMessagesPanel(); }
      else openRoomTarget(roomId, targetThreadId, messageId);
    }, () => openRoomTarget(roomId, targetThreadId, messageId));
  }, [listedRoomIds, lookupRoom, openRoomTarget, openSession]);
  // A failed-run toast (RunNotifications) opens the room the run ran in.
  useEffect(() => {
    const onOpenRoom = (event: Event) => {
      const detail = (event as CustomEvent<{ roomId?: string; threadId?: string | null; sessionId?: string }>).detail;
      // A session notification or Activity item carries a session instead of a room.
      if (detail?.sessionId) { openSession(detail.sessionId); activateMessagesPanel(); return; }
      if (detail?.roomId) openNotificationTarget(detail.roomId, detail.threadId ?? null);
    };
    window.addEventListener("openRoom", onOpenRoom);
    return () => window.removeEventListener("openRoom", onOpenRoom);
  }, [openNotificationTarget, openSession]);

  const unreadTotal = useMemo(() => rooms.rooms.reduce((sum, room) => sum + (room.muted ? 0 : room.unreadCount), 0), [rooms.rooms]);
  // The Dock and tab count also carry your own sessions waiting on you (gap 8).
  const { prefs: notifyPrefs } = useSessionNotificationPrefs();
  const needingYou = useMemo(() => (notifyPrefs.waiting === "off" ? 0 : ownWaitingCount(sessionRows)), [sessionRows, notifyPrefs]);

  const workspaceState = useWorkspaceState();
  const activeDirectoryRoom = directoryRooms.find((room) => room.roomId === rooms.activeRoomId);
  const tabTitle = placeTabTitle(workspaceState, sessionFocus ? "Session"
    : activeDirectoryRoom ? roomDisplayName(activeDirectoryRoom, activeDirectoryRoom.participants) : null);

  return <MessagingWorkspaceContext.Provider value={{
    activeRoomId: rooms.activeRoomId,
    directoryProps,
    roomProps,
    selectAgentDm,
    openMessage,
    openNotificationTarget,
    openSession,
    openNewSession,
    sessionFocus,
    closeSession,
    setSessionChat,
    setSessionChatRoomId,
    setSideRoom,
    sessionSource: rooms.resourceSource,
    resolveSessionOwner,
    workspaceSessions,
    scope,
    freshLanding,
    explicitPickCount,
    askLightbulb,
  }}><MessageNotifications activeRoomId={rooms.activeRoomId} threadId={threadId} roomVisible={roomVisible} sideRoomIds={sideRoomIds} rooms={rooms.rooms}
    onOpen={openNotificationTarget} activeSessionId={sessionFocus?.sessionId ?? null}
    onOpenSession={(sessionId) => { openSession(sessionId); activateMessagesPanel(); }} /><ShortcutSheet open={shortcutsOpen} onOpenChange={setShortcutsOpen} />{superAgent ? <NeedsYouTitleBadge title={tabTitle} /> : <UnreadTitleBadge unread={unreadTotal + needingYou} title={tabTitle} />}{children}
    {selectRoomError && <div role="alert" className="px-4 py-2 text-ui text-err">{selectRoomError}{" "}
      <button type="button" className="underline underline-offset-4" onClick={() => setSelectErrorDismissed(rooms.selectRoomError)}>Dismiss</button>
    </div>}</MessagingWorkspaceContext.Provider>;
}
